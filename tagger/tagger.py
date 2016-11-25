import os
import boto3
import botocore
from retrying import retry
import socket
import csv

def _arn_to_name(resource_arn):
    #Example: arn:aws:elasticloadbalancing:us-east-1:397853141546:loadbalancer/pb-adn-arc2
    parts = resource_arn.split(':')
    name = parts[-1]
    parts = name.split('/', 1)
    if len(parts) == 2:
        name = parts[-1]
    return name

def _format_dict(tags):
    output = []
    for key, value in tags.iteritems():
        output.append("%s:%s" % (key, value))

    return ", ".join(output)

def _dict_to_aws_tags(tags):
    return [{'Key': key, 'Value': value} for key, value in tags.iteritems() if not key.startswith('aws:') and not key.startswith('Name')]

def _aws_tags_to_dict(aws_tags):
    return {x['Key']: x['Value'] for x in aws_tags if not x['Key'].startswith('aws:')}

def _fetch_temporary_credentials(role):
    sts = boto3.client('sts', region_name=os.environ.get('AWS_REGION', 'us-east-1'))

    response = sts.assume_role(RoleArn=role, RoleSessionName='aws-tagger.%s' % socket.gethostname())
    access_key_id = response.get('Credentials', {}).get('AccessKeyId', None)
    secret_access_key = response.get('Credentials', {}).get('SecretAccessKey', None)
    session_token = response.get('Credentials', {}).get('SessionToken', None)
    return access_key_id, secret_access_key, session_token

def _client(name, role, region):
    kwargs = {}

    if region:
        kwargs['region_name'] = region
    elif os.environ.get('AWS_REGION'):
        kwargs['region_name'] = os.environ['AWS_REGION']

    if role:
        access_key_id, secret_access_key, session_token = _fetch_temporary_credentials(role)
        kwargs['aws_access_key_id'] = access_key_id
        kwargs['aws_secret_access_key'] = secret_access_key
        kwargs['aws_session_token'] = session_token

    return boto3.client(name, **kwargs)

class SingleResourceTagger(object):
    def __init__(self, dryrun, verbose, role=None, region=None):
        self.taggers = {}
        self.taggers['ec2'] = EC2Tagger(dryrun, verbose, role=role, region=region)
        self.taggers['elasticfilesystem'] = EFSTagger(dryrun, verbose, role=role, region=region)
        self.taggers['rds'] = RDSTagger(dryrun, verbose, role=role, region=region)
        self.taggers['elasticloadbalancing'] = LBTagger(dryrun, verbose, role=role, region=region)
        self.taggers['elasticache'] = ElasticacheTagger(dryrun, verbose, role=role, region=region)

    def tag(self, resource_id, tags):
        if resource_id == "":
            return

        tagger = None
        if resource_id.startswith('i-'):
            tagger = self.taggers['ec2']
        elif resource_id.startswith('arn:'):
            parts = resource_id.split(':')
            if len(parts) > 4:
                product = parts[2]
                tagger = self.taggers.get(product)

        if tagger:
            tagger.tag(resource_id, tags)
        else:
            print "Tagging is not support for this resource %s" % resource_id

class MultipleResourceTagger(object):
    def __init__(self, dryrun, verbose, role=None, region=None):
        self.tagger = SingleResourceTagger(dryrun, verbose, role=role, region=region)

    def tag(self, resource_ids, tags):
        for resource_id in resource_ids:
            self.tagger.tag(resource_id, tags)

class CSVResourceTagger(object):
    def __init__(self, dryrun, verbose, role=None, region=None):
        self.dryrun = dryrun
        self.verbose = verbose
        self.role = role
        self.region = region
        self.regional_tagger = {}
        self.resource_id_column = 'Id'
        self.region_column = 'Region'

    def tag(self, filename):
        with open(filename, 'rU') as csv_file:
            reader = csv.reader(csv_file)
            header_row = True
            tag_index = None

            for row in reader:
                if header_row:
                    header_row = False
                    tag_index = self._parse_header(row)
                else:
                    self._tag_resource(tag_index, row)

    def _parse_header(self, header_row):
        tag_index = {}
        for index, name in enumerate(header_row):
            tag_index[name] = index

        return tag_index

    def _tag_resource(self, tag_index, row):
        resource_id = row[tag_index[self.resource_id_column]]
        tags = {}
        for key, index in tag_index.iteritems():
            value = row[index]
            if key != self.resource_id_column and key != self.region_column and value != "":
                tags[key] = value

        tagger = self._lookup_tagger(tag_index, row)
        tagger.tag(resource_id, tags)

    def _lookup_tagger(self, tag_index, row):
        region = self.region
        region_index = tag_index.get(self.region_column)

        if region_index is not None:
            region = row[region_index]
        if region == '':
            region = None

        tagger = self.regional_tagger.get(region)
        if tagger is None:
            tagger = SingleResourceTagger(self.dryrun, self.verbose, role=self.role, region=region)
            self.regional_tagger[region] = tagger

        return tagger

class EC2Tagger(object):
    def __init__(self, dryrun, verbose, role=None, region=None):
        self.dryrun = dryrun
        self.verbose = verbose
        self.ec2 = _client('ec2', role=role, region=region)
        self.volume_cache = {}
        #TODO implement paging for describe instances
        reservations = self._ec2_describe_instances(MaxResults=1000)

        for reservation in reservations["Reservations"]:
            for instance in reservation["Instances"]:
                instance_id = instance['InstanceId']
                volumes = instance.get('BlockDeviceMappings', [])
                self.volume_cache[instance_id] = []
                for volume in volumes:
                    ebs = volume.get('Ebs', {})
                    volume_id = ebs.get('VolumeId')
                    if volume_id:
                        self.volume_cache[instance_id].append(volume_id)

    def tag(self, instance_id, tags):
        aws_tags = _dict_to_aws_tags(tags)
        resource_ids = [instance_id]
        resource_ids.extend(self.volume_cache.get(instance_id, []))
        if self.verbose:
            print "tagging %s with %s" % (", ".join(resource_ids), _format_dict(tags))
        if not self.dryrun:
            self._ec2_create_tags(Resources=resource_ids, Tags=aws_tags)

    def _is_retryable_exception(exception):
        return not isinstance(exception, botocore.exceptions.ClientError) or \
            (exception.response["Error"]["Code"] in ['RequestLimitExceeded'])

    @retry(retry_on_exception=_is_retryable_exception, stop_max_delay=30000, wait_exponential_multiplier=1000)
    def _ec2_describe_instances(self, **kwargs):
        return self.ec2.describe_instances(**kwargs)

    @retry(retry_on_exception=_is_retryable_exception, stop_max_delay=30000, wait_exponential_multiplier=1000)
    def _ec2_create_tags(self, **kwargs):
        return self.ec2.create_tags(**kwargs)

class EFSTagger(object):
    def __init__(self, dryrun, verbose, role=None, region=None):
        self.dryrun = dryrun
        self.verbose = verbose
        self.efs = _client('efs', role=role, region=region)

    def tag(self, resource_arn, tags):
        file_system_id = _arn_to_name(resource_arn)
        aws_tags = _dict_to_aws_tags(tags)

        if self.verbose:
            print "tagging %s with %s" % (file_system_id, _format_dict(tags))
        if not self.dryrun:
            self._efs_create_tags(FileSystemId=file_system_id, Tags=aws_tags)

    def _is_retryable_exception(exception):
        return not isinstance(exception, botocore.exceptions.ClientError) or \
            (exception.response["Error"]["Code"] in ['RequestLimitExceeded'])

    @retry(retry_on_exception=_is_retryable_exception, stop_max_delay=30000, wait_exponential_multiplier=1000)
    def _efs_create_tags(self, **kwargs):
        return self.efs.create_tags(**kwargs)

class RDSTagger(object):
    def __init__(self, dryrun, verbose, role=None, region=None):
        self.dryrun = dryrun
        self.verbose = verbose
        self.rds = _client('rds', role=role, region=region)

    def tag(self, resource_arn, tags):
        aws_tags = _dict_to_aws_tags(tags)
        if self.verbose:
            print "tagging %s with %s" % (resource_arn, _format_dict(tags))
        if not self.dryrun:
            try:
                self._rds_add_tags_to_resource(ResourceName=resource_arn, Tags=aws_tags)
            except botocore.exceptions.ClientError as exception:
                if exception.response["Error"]["Code"] in ['DBInstanceNotFound']:
                    print "Resource not found: %s" % resource_arn
                else:
                    raise exception

    def _is_retryable_exception(exception):
        return not isinstance(exception, botocore.exceptions.ClientError) or \
            (exception.response["Error"]["Code"] in ['RequestLimitExceeded'])

    @retry(retry_on_exception=_is_retryable_exception, stop_max_delay=30000, wait_exponential_multiplier=1000)
    def _rds_add_tags_to_resource(self, **kwargs):
        return self.rds.add_tags_to_resource(**kwargs)

class LBTagger(object):
    def __init__(self, dryrun, verbose, role=None, region=None):
        self.dryrun = dryrun
        self.verbose = verbose
        self.elb = _client('elb', role=role, region=region)
        self.alb = _client('elbv2', role=role, region=region)

    def tag(self, resource_arn, tags):
        elb_name = _arn_to_name(resource_arn)
        aws_tags = _dict_to_aws_tags(tags)

        if self.verbose:
            print "tagging %s with %s" % (resource_arn, _format_dict(tags))
        if not self.dryrun:
            try:
                if ':loadbalancer/app/' in resource_arn:
                    self._alb_add_tags(ResourceArns=[resource_arn], Tags=aws_tags)
                else:
                    self._elb_add_tags(LoadBalancerNames=[elb_name], Tags=aws_tags)
            except botocore.exceptions.ClientError as exception:
                if exception.response["Error"]["Code"] in ['LoadBalancerNotFound']:
                    print "Resource not found: %s" % resource_arn
                else:
                    raise exception

    def _is_retryable_exception(exception):
        return not isinstance(exception, botocore.exceptions.ClientError) or \
            (exception.response["Error"]["Code"] in ['RequestLimitExceeded'])

    @retry(retry_on_exception=_is_retryable_exception, stop_max_delay=30000, wait_exponential_multiplier=1000)
    def _elb_add_tags(self, **kwargs):
        return self.elb.add_tags(**kwargs)

    @retry(retry_on_exception=_is_retryable_exception, stop_max_delay=30000, wait_exponential_multiplier=1000)
    def _alb_add_tags(self, **kwargs):
        return self.alb.add_tags(**kwargs)

class ElasticacheTagger(object):
    def __init__(self, dryrun, verbose, role=None, region=None):
        self.dryrun = dryrun
        self.verbose = verbose
        self.elasticache = _client('elasticache', role=role, region=region)

    def tag(self, resource_arn, tags):
        aws_tags = _dict_to_aws_tags(tags)
        if self.verbose:
            print "tagging %s with %s" % (resource_arn, _format_dict(tags))
        if not self.dryrun:
            try:
                self._elasticache_add_tags_to_resource(ResourceName=resource_arn, Tags=aws_tags)
            except botocore.exceptions.ClientError as exception:
                if exception.response["Error"]["Code"] in ['CacheClusterNotFound']:
                    print "Resource not found: %s" % resource_arn
                else:
                    raise exception

    def _is_retryable_exception(exception):
        return not isinstance(exception, botocore.exceptions.ClientError) or \
            (exception.response["Error"]["Code"] in ['RequestLimitExceeded'])

    @retry(retry_on_exception=_is_retryable_exception, stop_max_delay=30000, wait_exponential_multiplier=1000)
    def _elasticache_add_tags_to_resource(self, **kwargs):
        return self.elasticache.add_tags_to_resource(**kwargs)

