
I have created a Cloudformation Template to help migrate customers from Transit VPC to Transit Gateway.

It takes the following inputs:

-Stack ARN of the Transit VPC CFN  stack from where the config is to be migrated.

-Configuration parameters of the TGW that will be created - AmazonSideAsn,AutoAcceptSharedAttachments,DefaultRouteTableAssociation,DefaultRouteTablePropagation,VpnEcmpSupport

When the Stack is created , it:

- Creates a TGW in the region where the stack is launched.

- Creates a IAM Role that will be assumed by the Lambda function

- Invokes a Lambda function using CFN Custom resource:

  - The Lambda function uses the transit VPC ARN (provided as input to the template) to scan all VPNs created in this region, by the transit VPC stack.
  
  - To do this is parses through the xml config files created by transit-vpc VGW poller function , stored in s3.
  
  - When it finds a VGW in this region, it creates a attachment from the VPC to the Transit Gateway created by CFN.
  
  - When it finds a VGW in a different account, it shares the TGW with the account.
  
- Outputs the TGW ID as the stack output.

When the stack is deleted:

  -It deletes all attachments to the TGW that was initially created and then deletes the TGW

As "AWS::Lambda::Function" resource can fetch code only from an S3 bucket in the same region as the function , the Lambda Zip file need to uploaded in all regions , so that customer can choose to launch their TGW in a region of their choice.

I am currently working on getting the Lambda zip file uploaded to public S3 buckets in different regions ,so that stack can be launched directly.

Until then you will need to manually upload the Lambda code file -TGW.zip (attached below) to an S3 bucket in your account and reference you bucket name in the CFN template(also attached below).

"TGWLambdaFunction": {
  "Type": "AWS::Lambda::Function",
  "Properties": {
    "Code": {
        "S3Bucket": "<bucket-name>", <<< Replace this with the name of your S3 bucket
        "S3Key" : "tgw-migrator/latest/TGW.zip" <<< Replace this with the Object key
      }
