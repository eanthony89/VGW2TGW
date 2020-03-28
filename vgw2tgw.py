#---------------------------------------------------------------------------------
# Lambda function to migrate existing customers from Transit VPC to Transit Gateway
# Version: 1.0
#---------------------------------------------------------------------------------

from botocore.client import Config
from xml.dom import minidom
import logging, operator, re, time, json, boto3, datetime
from botocore.vendored import requests
import botocore
from urllib.request import Request, urlopen
from os import environ

#------------------------------------------------------------------
# Set Log Level
#------------------------------------------------------------------
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    responseStatus = 'SUCCESS'
    reason = None
    responseData = {}
    resourceId = event['PhysicalResourceId'] if 'PhysicalResourceId' in event else event['LogicalResourceId']
    result = {
        'StatusCode': '200',
        'Body':  {'message': 'success'}
    }

    try:
        #----------------------------------------------------------
        # Process event
        #----------------------------------------------------------
        if event['RequestType'] == 'Delete':
            logger.info("DELETE event recieved")
            logger.info('REQUEST RECEIVED:\n %s', event)
            tgw_region=event['ResourceProperties']['Region']
            tgw_id=event['ResourceProperties']['TGWID']
            cloudformation_delete(event, context, tgw_id, tgw_region)
        elif event['RequestType'] == 'Create':
            logger.info("CREATE event recieved")
            logger.info('REQUEST RECEIVED:\n %s', event)
            tgw_region=event['ResourceProperties']['Region']
            tvpc_arn=event['ResourceProperties']['StackName']
            tgw_id=event['ResourceProperties']['TGWID']
            cloudformation_create(event, context, tvpc_arn, tgw_id, tgw_region)
        elif event['RequestType'] == 'Update':
            logger.info("UPDATE event recieved")
            logger.info('REQUEST RECEIVED:\n %s', event)
            tgw_id=event['ResourceProperties']['TGWID']
            tvpc_arn=event['ResourceProperties']['StackName']
            tgw_region=event['ResourceProperties']['Region']
            cloudformation_update(event, context, tvpc_arn, tgw_id, tgw_region)
    except Exception as error:
        logger.info('FAILED!')
        responseStatus = 'FAILED'
        reason = str(error)
        result = {
            'statusCode': '500',
            'body':  {'message': reason}
        }

    finally:
        #------------------------------------------------------------------
        # Send Result
        #------------------------------------------------------------------
        if 'ResponseURL' in event:
            send_response(event, context, responseStatus, responseData, reason, resourceId)

        return json.dumps(result)

def send_response(event, context, responseStatus, responseData, resourceId, reason=None):
    responseUrl = event['ResponseURL']
    responseBody = {}
    responseBody['Status'] = responseStatus
    responseBody['Reason'] = reason
    responseBody['PhysicalResourceId'] = resourceId
    responseBody['StackId'] = event['StackId']
    responseBody['RequestId'] = event['RequestId']
    responseBody['LogicalResourceId'] = event['LogicalResourceId']
    responseBody['PhysicalResourceId'] = event['PhysicalResourceId'] if 'PhysicalResourceId' in event else event['LogicalResourceId']
    responseBody['NoEcho'] = False
    responseBody['Data'] = responseData

    json_responseBody = json.dumps(responseBody)
    headers = {
        'content-type' : '',
        'content-length' : str(len(json_responseBody))
    }
    logger.info('ResponseURL: %s', event['ResponseURL'])
    logger.info('ResponseBody: %s', json_responseBody)
    try:
        response = requests.put(responseUrl,
                                data=json_responseBody,
                                headers=headers)

    except Exception as error:
        logger.info (error)


def cloudformation_create(event, context, tvpc_arn, tgw_id, tgw_region):
    try:
        shared_accounts = []
        tvpc_region,account_id=operator.itemgetter(3,4)(re.split(r':',tvpc_arn))
        cf= boto3.client('cloudformation',region_name=tvpc_region)
        describe_stack = cf.describe_stacks(StackName=tvpc_arn)
        #grab the bucket name and bucket prefix,  being used to store VPN config from Transit VPC stack output
        bucket_name= ((((describe_stack.get("Stacks"))[0]).get("Outputs"))[2]).get("OutputValue")
        bucket_prefix= ((((describe_stack.get("Stacks"))[0]).get("Outputs"))[3]).get("OutputValue")+"CSR1"
        s3 = boto3.client('s3',region_name=tvpc_region)
        #create list of vpn config file
        objects=s3.list_objects_v2(Bucket=bucket_name,Prefix=bucket_prefix)
        #create a ec2 resource in the region where TGW was created
        ec2=boto3.client('ec2',region_name=tgw_region)
        tgw_arn=ec2.describe_transit_gateways(TransitGatewayIds=[tgw_id])["TransitGateways"][0]["TransitGatewayArn"]
        #iterating over all VPN config files
        for obj in objects['Contents']:
            az_id = []
            spoke_subnets = []
            #parse XML VPN config file in S3 as a string
            config=s3.get_object(Bucket=bucket_name, Key=obj['Key'])['Body'].read().decode('utf-8')
            xmldoc =minidom.parseString(config)
            #parse xml to grab vgw_id and spoke_account numbers
            vpn_gateway_id=xmldoc.getElementsByTagName('vpn_gateway_id')[0].firstChild.data
            spoke_account_id=xmldoc.getElementsByTagName('account_id')[0].firstChild.data
            #Find the region in which the VGW is present by reading the name of the VPN config file
            spoke_region = obj['Key'].split("-vpn-")[0].split("/CSR1/")[1]
            #creating a ec2 resource in this region
            ec2=boto3.client('ec2',region_name=spoke_region)
            #As of now , TGW only supports attachments in the same region. I am checking if the VGW is present in the same region as the TGW
            #If VGW and TGW are in different regions , no attachments are created
            if (spoke_account_id == account_id and spoke_region == tgw_region):
                logger.info ("Found VGW-ID {0} located in REGION {1} for account-id {2}".format( vpn_gateway_id,spoke_region,spoke_account_id))
                #Grab the VPC ID from the VGW ID
                spoke_vpc_id=ec2.describe_vpn_gateways(VpnGatewayIds=[vpn_gateway_id])['VpnGateways'][0]['VpcAttachments'][0]['VpcId']
                if spoke_vpc_id != "":
                #For redundancy , the function creates one attachment per AZ , if the VPC has subnets in a AZ.
                    all_subnets = ec2.describe_subnets(Filters=[{'Name': 'vpc-id','Values': [spoke_vpc_id]}])
                    for i in all_subnets['Subnets']:
                        if i['AvailabilityZone'] not in az_id:
                            az_id.append(i['AvailabilityZone'])
                            spoke_subnets.append (i['SubnetId'])
                    if spoke_subnets != []:
                        try:
                            ec2=boto3.client('ec2',region_name=tgw_region)
                            ec2.create_transit_gateway_vpc_attachment(TransitGatewayId=tgw_id,VpcId=spoke_vpc_id,SubnetIds=spoke_subnets)
                        except Exception as error:
                            logger.info (error)

                    else:
                        logger.info ("There are no subnets in {0} to create attachments to the TGW . Skipping.".format(spoke_vpc_id))
                else :
                    logger.info ("There is no VPC binded to the VGW {0}.Skipping.".format(vpn_gateway_id))
            elif (spoke_account_id != account_id):
                logger.info ("Found VGW-ID {0} located in region {1} for account-id {2}".format( vpn_gateway_id,spoke_region,spoke_account_id))
                shared_accounts.append(spoke_account_id)
            #Create a RAM resource to share the TGW with cross accounts
        if shared_accounts !=[]:
            logger.info ("Sharing TGW with accounts {0}".format(shared_accounts))
            client = boto3.client('ram', region_name=tgw_region)
            ram = client.create_resource_share(name="share_tgw",resourceArns=[tgw_arn],principals=shared_accounts,allowExternalPrincipals=True)
        else:
            logger.info ("The TGW was not shared with any other account as the transit VPC stack had no crosss account VPNs")
    except Exception as error:
        logger.info (error)


#Delete Logic to delete TGW attachments and then the TGW
def cloudformation_delete(event, context, tgw_id, tgw_region):
    try:
        ec2=boto3.client('ec2',region_name=tgw_region)
        response = ec2.describe_transit_gateway_attachments(Filters=[{'Name':'transit-gateway-id','Values':[tgw_id]}])["TransitGatewayAttachments"]
        if response != []:
            for i in response:
                ec2.delete_transit_gateway_vpc_attachment(TransitGatewayAttachmentId=i["TransitGatewayAttachmentId"])
                logger.info ("Detaching attachment-id {0} from TGW-id {1} ".format( i["TransitGatewayAttachmentId"],tgw_id))
        else:
            logger.info ("TGW-id {0} has no attachments to delete".format(tgw_id))
    except Exception as error:
        logger.info (error)
    #sleeping for 15 seconds to allow time for all attachments to get deleted before CFN stack attempts to delete the TGW
    time.sleep(15)

#Update Logic to create new TGW attachments with the new TGW
def cloudformation_update(event, context, tvpc_arn, tgw_id, tgw_region):
    try:
        shared_accounts = []
        tvpc_region,account_id=operator.itemgetter(3,4)(re.split(r':',tvpc_arn))
        cf= boto3.client('cloudformation',region_name=tvpc_region)
        describe_stack = cf.describe_stacks(StackName=tvpc_arn)
        #grab the bucket name and bucket prefix,  being used to store VPN config from Transit VPC stack output
        bucket_name= ((((describe_stack.get("Stacks"))[0]).get("Outputs"))[2]).get("OutputValue")
        bucket_prefix= ((((describe_stack.get("Stacks"))[0]).get("Outputs"))[3]).get("OutputValue")+"CSR1"
        s3 = boto3.client('s3',region_name=tvpc_region)
        #create list of vpn config file
        objects=s3.list_objects_v2(Bucket=bucket_name,Prefix=bucket_prefix)
        #create a ec2 resource in the region where TGW was created
        ec2=boto3.client('ec2',region_name=tgw_region)
        tgw_arn=ec2.describe_transit_gateways(TransitGatewayIds=[tgw_id])["TransitGateways"][0]["TransitGatewayArn"]
        #iterating over all VPN config files
        for obj in objects['Contents']:
            az_id = []
            spoke_subnets = []
            #parse XML VPN config file in S3 as a string
            config=s3.get_object(Bucket=bucket_name, Key=obj['Key'])['Body'].read().decode('utf-8')
            xmldoc =minidom.parseString(config)
            #parse xml to grab vgw_id and spoke_account numbers
            vpn_gateway_id=xmldoc.getElementsByTagName('vpn_gateway_id')[0].firstChild.data
            spoke_account_id=xmldoc.getElementsByTagName('account_id')[0].firstChild.data
            #Find the region in which the VGW is present by reading the name of the VPN config file
            spoke_region = obj['Key'].split("-vpn-")[0].split("/CSR1/")[1]
            #creating a ec2 resource in this region
            ec2=boto3.client('ec2',region_name=spoke_region)
            #As of now , TGW only supports attachments in the same region. I am checking if the VGW is present in the same region as the TGW
            #If VGW and TGW are in different regions , no attachments are created
            if (spoke_account_id == account_id and spoke_region == tgw_region):
                logger.info ("Found VGW-ID {0} located in REGION {1} for account-id {2}".format( vpn_gateway_id,spoke_region,spoke_account_id))
                #Grab the VPC ID from the VGW ID
                spoke_vpc_id=ec2.describe_vpn_gateways(VpnGatewayIds=[vpn_gateway_id])['VpnGateways'][0]['VpcAttachments'][0]['VpcId']
                if spoke_vpc_id != "":
                #For redundancy , the function creates one attachment per AZ , if the VPC has subnets in a AZ.
                    all_subnets = ec2.describe_subnets(Filters=[{'Name': 'vpc-id','Values': [spoke_vpc_id]}])
                    for i in all_subnets['Subnets']:
                        if i['AvailabilityZone'] not in az_id:
                            az_id.append(i['AvailabilityZone'])
                            spoke_subnets.append (i['SubnetId'])
                    if spoke_subnets != []:
                        try:
                            ec2=boto3.client('ec2',region_name=tgw_region)
                            ec2.create_transit_gateway_vpc_attachment(TransitGatewayId=tgw_id,VpcId=spoke_vpc_id,SubnetIds=spoke_subnets)
                        except Exception as error:
                            logger.info (error)

                    else:
                        logger.info ("There are no subnets in {0} to create attachments to the TGW . Skipping.".format(spoke_vpc_id))
                else :
                    logger.info ("There is no VPC binded to the VGW {0}.Skipping.".format(vpn_gateway_id))
            elif (spoke_account_id != account_id):
                logger.info ("Found VGW-ID {0} located in region {1} for account-id {2}".format( vpn_gateway_id,spoke_region,spoke_account_id))
                shared_accounts.append(spoke_account_id)
        #Create a RAM resource to share the TGW with cross accounts
        if shared_accounts !=[]:
            logger.info ("Sharing TGW with accounts {0}".format(shared_accounts))
            client = boto3.client('ram', region_name=tgw_region)
            ram = client.create_resource_share(name="share_tgw",resourceArns=[tgw_arn],principals=shared_accounts,allowExternalPrincipals=True)
        else:
            logger.info ("The TGW was not shared with any other account as the transit VPC stack had no crosss account VPNs")
    except Exception as error:
        logger.info (error)
