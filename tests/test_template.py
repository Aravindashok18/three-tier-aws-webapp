"""Checks on infra/three-tier.yaml.

CloudFormation cannot be deployed from a test, so these tests read the template and
assert the properties that make it a three-tier design: the database is unreachable
from the internet, only the load balancer is open to the world, nothing holds a
wildcard IAM permission, and so on. cfn-lint covers the syntax and resource schemas.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "infra" / "three-tier.yaml"


class CfnLoader(yaml.SafeLoader):
    """Reads CloudFormation's !Ref / !Sub / !GetAtt short forms as plain {"Fn::X": ...} dicts."""


def _intrinsic(loader, suffix, node):
    key = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
        if suffix == "GetAtt":
            value = value.split(".", 1)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {key: value}


CfnLoader.add_multi_constructor("!", _intrinsic)


@pytest.fixture(scope="module")
def template():
    return yaml.load(TEMPLATE.read_text(), Loader=CfnLoader)


def of_type(template, resource_type):
    return {name: r for name, r in template["Resources"].items() if r["Type"] == resource_type}


def ref(name):
    return {"Ref": name}


def all_ingress_rules(template):
    """(security group name, rule dict) for inline and stand-alone ingress rules."""
    rules = []
    for name, group in of_type(template, "AWS::EC2::SecurityGroup").items():
        rules.extend((name, rule) for rule in group["Properties"].get("SecurityGroupIngress", []))
    for resource in of_type(template, "AWS::EC2::SecurityGroupIngress").values():
        props = dict(resource["Properties"])
        group = props.pop("GroupId")["Ref"]
        rules.append((group, props))
    return rules


def all_egress_rules(template):
    rules = []
    for name, group in of_type(template, "AWS::EC2::SecurityGroup").items():
        rules.extend((name, rule) for rule in group["Properties"].get("SecurityGroupEgress", []))
    for resource in of_type(template, "AWS::EC2::SecurityGroupEgress").values():
        props = dict(resource["Properties"])
        group = props.pop("GroupId")["Ref"]
        rules.append((group, props))
    return rules


# --- syntax -------------------------------------------------------------------------

def test_cfn_lint_finds_nothing():
    linter = shutil.which("cfn-lint", path=str(Path(sys.executable).parent))
    if linter is None:
        pytest.skip("cfn-lint is not installed (pip install -r requirements-dev.txt)")

    result = subprocess.run([linter, str(TEMPLATE)], capture_output=True, text=True)

    assert result.returncode == 0, result.stdout + result.stderr


# --- data tier ----------------------------------------------------------------------

def test_database_is_private_encrypted_and_has_no_password_in_the_template(template):
    props = template["Resources"]["Database"]["Properties"]

    assert props["PubliclyAccessible"] is False
    assert props["StorageEncrypted"] is True
    assert props["ManageMasterUserPassword"] is True
    assert "MasterUserPassword" not in props
    assert props["VPCSecurityGroups"] == [ref("DbSecurityGroup")]


def test_database_subnet_group_uses_only_private_subnets(template):
    subnets = template["Resources"]["DbSubnetGroup"]["Properties"]["SubnetIds"]

    assert subnets == [ref("PrivateSubnetA"), ref("PrivateSubnetB")]


def test_private_subnets_have_no_route_to_the_internet(template):
    for association in ("PrivateSubnetARouteTableAssociation", "PrivateSubnetBRouteTableAssociation"):
        assert template["Resources"][association]["Properties"]["RouteTableId"] == ref("PrivateRouteTable")
    routes = of_type(template, "AWS::EC2::Route")
    assert [r for r in routes.values() if r["Properties"]["RouteTableId"] == ref("PrivateRouteTable")] == []
    assert all(r["Properties"]["MapPublicIpOnLaunch"] is False
               for n, r in of_type(template, "AWS::EC2::Subnet").items() if n.startswith("Private"))


def test_database_accepts_mysql_from_the_app_group_only(template):
    rules = [rule for group, rule in all_ingress_rules(template) if group == "DbSecurityGroup"]

    assert len(rules) == 1
    assert (rules[0]["FromPort"], rules[0]["ToPort"]) == (3306, 3306)
    assert rules[0]["SourceSecurityGroupId"] == ref("AppSecurityGroup")
    assert "CidrIp" not in rules[0]


def test_database_refuses_unencrypted_connections(template):
    group = template["Resources"]["DbParameterGroup"]["Properties"]

    assert group["Parameters"]["require_secure_transport"] == "1"
    assert template["Resources"]["Database"]["Properties"]["DBParameterGroupName"] == ref("DbParameterGroup")


def test_database_has_no_outbound_access(template):
    rules = [rule for group, rule in all_egress_rules(template) if group == "DbSecurityGroup"]

    assert [r["CidrIp"] for r in rules] == ["127.0.0.1/32"]  # the placeholder that removes allow-all


# --- web and app tiers ------------------------------------------------------------------

def test_only_the_load_balancer_is_open_to_the_internet(template):
    open_rules = [(group, rule) for group, rule in all_ingress_rules(template) if rule.get("CidrIp") == "0.0.0.0/0"]

    assert open_rules, "the load balancer must accept traffic"
    assert {group for group, _ in open_rules} == {"AlbSecurityGroup"}
    assert {rule["FromPort"] for _, rule in open_rules} <= {80, 443}


def test_app_instances_take_http_from_the_load_balancer_and_ssh_from_the_admin_only(template):
    rules = [rule for group, rule in all_ingress_rules(template) if group == "AppSecurityGroup"]
    by_port = {rule["FromPort"]: rule for rule in rules}

    assert set(by_port) == {22, 80}
    assert by_port[80]["SourceSecurityGroupId"] == ref("AlbSecurityGroup")
    assert by_port[22]["CidrIp"] == ref("AdminCidr")
    assert all(rule["CidrIp"] != "0.0.0.0/0" for rule in rules if "CidrIp" in rule)


def test_ssh_rule_disappears_without_an_admin_address(template):
    assert template["Resources"]["AppSshIngress"]["Condition"] == "HasAdminCidr"
    assert template["Conditions"]["HasAdminCidr"] == {"Fn::Not": [{"Fn::Equals": [ref("AdminCidr"), ""]}]}


def test_admin_address_cannot_be_the_whole_internet(template):
    assertion = template["Rules"]["SshNotOpenToTheWorld"]["Assertions"][0]["Assert"]

    assert assertion == {"Fn::Not": [{"Fn::Equals": [ref("AdminCidr"), "0.0.0.0/0"]}]}
    pattern = template["Parameters"]["AdminCidr"]["AllowedPattern"]
    assert re.fullmatch(pattern, "203.0.113.7/32")
    assert re.fullmatch(pattern, "")
    assert not re.fullmatch(pattern, "0.0.0.0/0")  # /0 is rejected by the pattern as well
    assert not re.fullmatch(pattern, "203.0.113.7")


def test_load_balancer_can_only_forward_to_the_app_group(template):
    rules = [rule for group, rule in all_egress_rules(template) if group == "AlbSecurityGroup"]

    real = [r for r in rules if r.get("CidrIp") != "127.0.0.1/32"]
    assert len(real) == 1
    assert real[0]["DestinationSecurityGroupId"] == ref("AppSecurityGroup")
    assert real[0]["FromPort"] == 80


def test_app_instances_reach_only_the_database_and_web_ports_outbound(template):
    rules = [rule for group, rule in all_egress_rules(template) if group == "AppSecurityGroup"]

    destinations = {
        (r["FromPort"], r.get("CidrIp") or "sg:" + r["DestinationSecurityGroupId"]["Ref"]) for r in rules
    }
    assert destinations == {(80, "0.0.0.0/0"), (443, "0.0.0.0/0"), (3306, "sg:DbSecurityGroup")}


def test_every_security_group_rule_explains_itself(template):
    for _group, rule in all_ingress_rules(template) + all_egress_rules(template):
        assert rule.get("Description"), rule


def test_instances_need_imdsv2_and_have_encrypted_disks(template):
    data = template["Resources"]["LaunchTemplate"]["Properties"]["LaunchTemplateData"]

    assert data["MetadataOptions"]["HttpTokens"] == "required"
    assert data["MetadataOptions"]["HttpPutResponseHopLimit"] == 1
    assert all(m["Ebs"]["Encrypted"] is True for m in data["BlockDeviceMappings"])
    assert data["SecurityGroupIds"] == [ref("AppSecurityGroup")]


def test_auto_scaling_group_spans_two_zones_and_trusts_the_load_balancer(template):
    group = template["Resources"]["AutoScalingGroup"]["Properties"]

    assert group["VPCZoneIdentifier"] == [ref("PublicSubnetA"), ref("PublicSubnetB")]
    assert group["HealthCheckType"] == "ELB"
    assert group["TargetGroupARNs"] == [ref("TargetGroup")]
    zones = [template["Resources"][s]["Properties"]["AvailabilityZone"] for s in ("PublicSubnetA", "PublicSubnetB")]
    assert zones[0] != zones[1]


def test_https_listener_exists_only_with_a_certificate(template):
    assert template["Resources"]["HttpsListener"]["Condition"] == "HasCertificate"
    assert template["Resources"]["AlbHttpsIngress"]["Condition"] == "HasCertificate"
    action = template["Resources"]["HttpListener"]["Properties"]["DefaultActions"][0]["Fn::If"]
    assert action[0] == "HasCertificate"
    assert action[1]["Type"] == "redirect"
    assert action[2]["Type"] == "forward"


# --- storage and permissions ------------------------------------------------------------------

def test_bucket_is_private_encrypted_versioned_and_retained(template):
    bucket = template["Resources"]["AppBucket"]
    props = bucket["Properties"]

    assert props["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True,
    }
    assert props["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    assert props["VersioningConfiguration"]["Status"] == "Enabled"
    assert bucket["DeletionPolicy"] == "Retain"


def test_bucket_policy_rejects_plain_http(template):
    statement = template["Resources"]["AppBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"][0]

    assert statement["Effect"] == "Deny"
    assert statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}


def test_instance_role_has_no_wildcards(template):
    role = template["Resources"]["InstanceRole"]["Properties"]

    assert role["ManagedPolicyArns"] == ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"]
    for policy in role["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            resources = statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
            assert all("*" not in action for action in actions), actions
            assert "*" not in resources, resources


def test_instance_role_can_read_but_not_write_the_bucket(template):
    actions = set()
    for policy in template["Resources"]["InstanceRole"]["Properties"]["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions.update([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"])

    assert actions == {"s3:GetObject", "s3:ListBucket", "secretsmanager:GetSecretValue"}


# --- the contract with the scripts ---------------------------------------------------------------

def test_user_data_passes_everything_bootstrap_requires(template):
    bootstrap = (ROOT / "scripts" / "bootstrap.sh").read_text()
    required = re.search(r'^: (.*)$', bootstrap, re.M).group(1)
    names = re.findall(r'"\$\{(\w+):\?\}"', required)
    assert names, "could not read the required variables from bootstrap.sh"

    user_data = template["Resources"]["LaunchTemplate"]["Properties"]["LaunchTemplateData"]["UserData"]["Fn::Base64"]["Fn::Sub"][0]

    for name in names:
        assert re.search(rf"^\s*{name}=", user_data, re.M), f"user data does not set {name}"
    assert "bootstrap.sh" in user_data


def test_outputs_include_what_deploy_script_reads(template):
    deploy = (ROOT / "scripts" / "deploy.sh").read_text()
    wanted = set(re.findall(r"\$\(output (\w+)\)", deploy))

    assert {"AppBucketName", "AutoScalingGroupName", "LoadBalancerUrl"} <= wanted
    assert wanted <= set(template["Outputs"])


def test_nginx_config_exposes_the_paths_the_template_and_cron_rely_on():
    nginx = (ROOT / "config" / "nginx-webapp.conf.template").read_text()
    template = TEMPLATE.read_text()
    healthcheck = (ROOT / "scripts" / "healthcheck.sh").read_text()

    assert "HealthCheckPath: /health" in template  # forwarded to the app by the catch-all location
    assert "location /static/" in nginx
    assert "location = /nginx-health" in nginx and "/nginx-health" in healthcheck
    assert "127.0.0.1:8000" in nginx
    assert "127.0.0.1:8000" in (ROOT / "config" / "webapp.service").read_text()
