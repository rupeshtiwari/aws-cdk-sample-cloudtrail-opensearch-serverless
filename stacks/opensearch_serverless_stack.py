"""
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""

from aws_cdk import (
    Stack,
    Duration,
    aws_ec2 as ec2,
    aws_cloudtrail as ct,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as cwl,
    aws_logs_destinations as cwl_destinations,
    aws_opensearchserverless as opensearchserverless,
    RemovalPolicy,
)
from aws_cdk.aws_s3_assets import Asset
from constructs import Construct


## Constants 
LOG_GROUP_NAME = "handler/svl_cloudtrail_logs"
COLLECTION_NAME = "ctcollection"
CWL_RETENTION = cwl.RetentionDays.THREE_DAYS
ENCRYPTIONPOLICY = f"""{{"Rules":[{{"ResourceType":"collection","Resource":["collection/{COLLECTION_NAME}"]}}],"AWSOwnedKey":true}}"""
NETWORKPOLICY = f"""[{{"Description":"Endpoint access for Lambda and for random querying","SourceVPCEs":["VPCENDPOINTID"],"Rules":[{{"ResourceType":"collection","Resource":["collection/{COLLECTION_NAME}"]}}],"AllowFromPublic":false}},{{"Description":"Dashboards access","AllowFromPublic":true,"Rules":[{{"ResourceType":"dashboard","Resource":["collection/{COLLECTION_NAME}"]}}]}}]"""
DATAPOLICY = f"""[
  {{
    "Description": "Endpoint access for Lambda and for random querying",
    "Rules":[
        {{
          "ResourceType":"collection",
          "Resource":["collection/{COLLECTION_NAME}"],
          "Permission":["aoss:*"]
        }},
        {{
          "ResourceType":"index",
          "Resource":["index/*/*"],
          "Permission":["aoss:*"]
        }}
    ],
    "Principal":["arn:aws:iam::147228461610:user/admin", "LAMBDAROLEARN"]
  }}
]
"""


class OpensearchServerlessStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        ################################################################################
        # VPC
        vpc = ec2.Vpc(self, "SvlCTCWLVpc")
        es_sec_grp = ec2.SecurityGroup(
            self,
            "SvlCTCWLOpenSearchSecGrp",
            vpc=vpc,
            allow_all_outbound=True,
            security_group_name="SvlCTCWLSecGrp",
        )
        es_sec_grp.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80))
        es_sec_grp.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443))

        endpoint = opensearchserverless.CfnVpcEndpoint(
            self,
            "SvlCTCWLEndpoint",
            name="svlctcwlendpoint",
            vpc_id=vpc.vpc_id,
            security_group_ids=[es_sec_grp.security_group_id],
            subnet_ids=[s.subnet_id for s in vpc.public_subnets],
        )

        # endpoint = ec2.VpcEndpoint(self, "ServerlessCtCWLVPCE")

        ###############################################################################
        # Amazon OpenSearch Serverless collection
        network_policy = NETWORKPOLICY.replace("VPCENDPOINTID", endpoint.attr_id)
        net = opensearchserverless.CfnSecurityPolicy(
            self,
            "SvlCTCWLNetwork",
            name="svlctcwlnetwork",
            description=f"Open access for {COLLECTION_NAME}",
            type="network",
            policy=network_policy,
        )

        sec = opensearchserverless.CfnSecurityPolicy(
            self,
            "SvlCTCWLEncryption",
            name="svlctcwlencryption",
            description="AWS Owned key policy for {COLLECTION_NAME}",
            type="encryption",
            policy=ENCRYPTIONPOLICY,
        )

        col = opensearchserverless.CfnCollection(
            self, COLLECTION_NAME, name=COLLECTION_NAME, type="TIMESERIES"
        )
        col.add_dependency(sec)

        ################################################################################
        # define a Lambda Layer for boto3
        boto3_lambda_layer = lambda_.LayerVersion(
            self,
            "Boto3LambdaLayer",
            code=lambda_.AssetCode("boto3-layer/"),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_9],
        )

        # Lambda for subscription filter
        subscription_filter_lambda = lambda_.Function(
            self,
            "build_os_client_and_bulk_ingest_logevents_handler",
            function_name="build_os_client_and_bulk_ingest_logevents_handler",
            runtime=lambda_.Runtime.PYTHON_3_9,
            code=lambda_.Code.from_asset(
                "lambda/build_os_client_and_bulk_ingest_logevents_handler"
            ),
            handler="index.handler",
            vpc=vpc,
            memory_size=1024,
            layers=[boto3_lambda_layer],
            timeout=Duration.minutes(5),
        )

        # Load Amazon OpenSearch Service Collection to env variable
        collection_endpoint = col.attr_collection_endpoint.replace("https://", "")
        print(f"\n\nCollection endpoint: {collection_endpoint}\n")
        subscription_filter_lambda.add_environment(
            "COLLECTION_ENDPOINT", collection_endpoint
        )
        subscription_filter_lambda.add_environment("REGION", self.region)
        subscription_filter_lambda.add_to_role_policy(
            iam.PolicyStatement(actions=["aoss:*"], resources=["*"])
        )
        subscription_filter_lambda.add_to_role_policy(
            iam.PolicyStatement(actions=["logs:*"], resources=["*"])
        )

        #################################################################################
        # The data access policy needs the lambda role ARN to allow writing.
        dap = DATAPOLICY.replace(
            "LAMBDAROLEARN", subscription_filter_lambda.role.role_arn
        )
        dat = opensearchserverless.CfnAccessPolicy(
            self,
            "SvlCTCWLData",
            name="svlctcwldata",
            type="data",
            description=f"Data access for {COLLECTION_NAME}",
            policy=dap,
        )

        ################################################################################
        # CWL Log Group
        log_group = cwl.LogGroup(
            self,
            "SvlCTCWLLogGroup",
            log_group_name=LOG_GROUP_NAME,
            retention=CWL_RETENTION,
            removal_policy=RemovalPolicy.DESTROY,
        )

        ################################################################################
        # CloudTrail trail
        trail = ct.Trail(
            self,
            "SvlCTCWLTrail",
            send_to_cloud_watch_logs=True,
            cloud_watch_log_group=log_group,
        )

        ################################################################################
        # Set up subscription filter
        subscription_filter = cwl.SubscriptionFilter(
            self,
            "SvlCTCWLSubFilter",
            log_group=log_group,
            destination=cwl_destinations.LambdaDestination(subscription_filter_lambda),
            filter_pattern=cwl.FilterPattern.all_events(),
        )

 