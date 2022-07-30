#!/usr/bin/python3

from genericpath import exists
import sys
import boto3
#import boto.sts
import boto.s3
import requests
import getpass
import configparser
import base64
import logging
import xml.etree.ElementTree as ET
import re
from bs4 import BeautifulSoup
from os.path import expanduser, isdir
from os import makedirs, getenv
from urllib.parse import urlparse, urlunparse
from botocore import UNSIGNED
from botocore.config import Config
from environs import Env

env = Env()
env.read_env()



##########################################################################
# Variables
try:
    accountMap = env.dict('ACCOUNT_MAP', subcast=str)
except:
    accountMap = {}
print(accountMap)

# region: The default AWS region that this script will connect
# to for all API calls
region = 'us-gov-east-1'

# output format: The AWS CLI output format that will be configured in the
# saml profile (affects subsequent CLI calls)
outputformat = 'json'

# awsconfigfile: The file where this script will store the temp
# credentials under the saml profile
awsconfigfile = '/.aws/credentials'

# SSL certificate verification: Whether or not strict certificate
# verification is done, False should only be used for dev/test
sslverification = True

# idpentryurl: The initial url that starts the authentication process.
#   EX:  
#     Commercial: idpentryurl = 'https://<fqdn>:<port>/adfs/ls/IdpInitiatedSignOn.aspx?loginToRp=urn:amazon:webservices'
#     GovCloud: idpentryurl = 'https://<fqdn>:<port>/adfs/ls/IdpInitiatedSignOn.aspx?loginToRp=urn:amazon:webservices:govcloud'
idpentryurl = env.str('IDENTITY_URL', 0)
print(idpentryurl)
#


# Uncomment to enable low level debugging
#logging.basicConfig(level=logging.DEBUG)

if len(sys.argv)>1:
    # Check for custom profile naming, perform prior to clean to make sure that we clean the correct section
    if '--profile' in sys.argv:
        profileLocation = sys.argv.index('--profile')
        if len(sys.argv) <= profileLocation+1:
            print("Invalid parameters.  Usage: SignIn.py [clean] [--profile <Profile Name>]")
            exit()
        else:
            sectionName = sys.argv[profileLocation+1]
    else:
        # default to original saml section
        sectionName='saml'

    if 'clean' in sys.argv:
        home = expanduser("~")
        filename = home + awsconfigfile

        # Read in the existing config file
        config = configparser.RawConfigParser()
        config.read(filename)

        # Put the credentials into a saml specific section instead of clobbering
        # the default credentials
        if config.has_section(sectionName):
            config.remove_section(sectionName)
            print("Saved credentials will be removed.")
        # Write the updated config file
        with open(filename, 'w+') as configfile:
            config.write(configfile)
            print("Saved credentials were removed")
        exit()
else:
    sectionName='saml'

##########################################################################

# Get the federated credentials from the user
print("Username:", end=' ')
username = input()
password = getpass.getpass()
print('')

# Initiate session handler
session = requests.Session()

# Programmatically get the SAML assertion
# Opens the initial IdP url and follows all of the HTTP302 redirects, and
# gets the resulting login page
formresponse = session.get(idpentryurl, verify=sslverification)
# Capture the idpauthformsubmiturl, which is the final url after all the 302s
idpauthformsubmiturl = formresponse.url

# Parse the response and extract all the necessary values
# in order to build a dictionary of all of the form values the IdP expects
formsoup = BeautifulSoup(formresponse.text, features="html.parser")
payload = {}

for inputtag in formsoup.find_all(re.compile('(INPUT|input)')):
    name = inputtag.get('name','')
    value = inputtag.get('value','')
    if "user" in name.lower():
        #Make an educated guess that this is the right field for the username
        payload[name] = username
    elif "email" in name.lower():
        #Some IdPs also label the username field as 'email'
        payload[name] = username
    elif "pass" in name.lower():
        #Make an educated guess that this is the right field for the password
        payload[name] = password
    elif "authmethod" in name.lower():
        payload[name] = "FormsAuthentication"
    else:
        #Simply populate the parameter with the existing value (picks up hidden fields in the login form)
        payload[name] = value

# Debug the parameter payload if needed
# Use with caution since this will print sensitive output to the screen
#print payload

# Some IdPs don't explicitly set a form action, but if one is set we should
# build the idpauthformsubmiturl by combining the scheme and hostname 
# from the entry url with the form action target
# If the action tag doesn't exist, we just stick with the 
# idpauthformsubmiturl above
for inputtag in formsoup.find_all(re.compile('(FORM|form)')):
    action = inputtag.get('action')
    loginid = inputtag.get('id')
    if (action and loginid == "loginForm"):
        parsedurl = urlparse(idpentryurl)
        idpauthformsubmiturl = parsedurl.scheme + "://" + parsedurl.netloc + action

#print(payload)
# Performs the submission of the IdP login form with the above post data
response = session.post(
    idpauthformsubmiturl, data=payload, verify=sslverification)

# Debug the response if needed
#print (response.text)

# Overwrite and delete the credential variables, just for safety
username = '##############################################'
password = '##############################################'
del username
del password

# Decode the response and extract the SAML assertion
soup = BeautifulSoup(response.text, features="html.parser")
assertion = ''

# Look for the SAMLResponse attribute of the input tag (determined by
# analyzing the debug print lines above)
for inputtag in soup.find_all('input'):
    if(inputtag.get('name') == 'SAMLResponse'):
        #print(inputtag.get('value'))
        assertion = inputtag.get('value')

# Better error handling is required for production use.
if (assertion == ''):
    #TODO: Insert valid error checking/handling
    print('Response did not contain a valid SAML assertion')
    sys.exit(0)

# Debug only
# print(base64.b64decode(assertion))

# Parse the returned assertion and extract the authorized roles
awsroles = []
root = ET.fromstring(base64.b64decode(assertion))
for saml2attribute in root.iter('{urn:oasis:names:tc:SAML:2.0:assertion}Attribute'):
    if (saml2attribute.get('Name') == 'https://aws.amazon.com/SAML/Attributes/Role'):
        for saml2attributevalue in saml2attribute.iter('{urn:oasis:names:tc:SAML:2.0:assertion}AttributeValue'):
            awsroles.append(saml2attributevalue.text)

# Note the format of the attribute value should be role_arn,principal_arn
# but lots of blogs list it as principal_arn,role_arn so let's reverse
# them if needed

for awsrole in awsroles:
    chunks = awsrole.split(',')
    if'saml-provider' in chunks[0]:
        newawsrole = chunks[1] + ',' + chunks[0]
        index = awsroles.index(awsrole)
        awsroles.insert(index, newawsrole)
        awsroles.remove(awsrole)

# If I have more than one role, ask the user which one they want,
# otherwise just proceed
print("")


if len(awsroles) > 1:

    i = 0
    print("Please choose the role you would like to assume:")
    strPadding = 20
    filler = ' '
    for awsrole in awsroles:
        #Get account number
        temp = awsrole.split(",")[0]
        #If account is mapped return account name/role, else return role arn with acct #
        if str(accountMap.get(temp[20:32],0)):
            print(
                '[', i, ']: ', 
                'Env: ', 
                str(accountMap.get(temp[20:32])).ljust(strPadding, filler), 
                temp[33:])
        else:
            print('[', i, ']: ', awsrole.split(',')[0])
        i += 1
    print("Selection: ", end=' ')
    selectedroleindex = input()

    # Basic sanity check of input
    if int(selectedroleindex) > (len(awsroles) - 1):
        print('You selected an invalid role index, please try again')
        sys.exit(0)

    role_arn = awsroles[int(selectedroleindex)].split(',')[0]
    principal_arn = awsroles[int(selectedroleindex)].split(',')[1]
else:
    role_arn = awsroles[0].split(',')[0]
    principal_arn = awsroles[0].split(',')[1]

# Use the assertion to get an AWS STS token using Assume Role with SAML
if env.bool('IS_PRIVATE_VPC', False):
    client=boto3.client('sts',region_name='us-gov-east-1', config=Config(signature_version=UNSIGNED), endpoint_url=env.str('PRIVATE_ENDPOINT_URL'))
else:
    client=boto3.client('sts',region_name='us-gov-east-1', config=Config(signature_version=UNSIGNED))

#conn = boto3.sts.connect_to_region(region)
token = client.assume_role_with_saml(RoleArn=role_arn, PrincipalArn=principal_arn, SAMLAssertion=assertion)

# Write the AWS STS token into the AWS credential file
home = expanduser("~")
filename = home + awsconfigfile

awsPath = home + '/.aws'
if not isdir(awsPath):
   makedirs(awsPath)

# Read in the existing config file
config = configparser.RawConfigParser()
config.read(filename)

# Put the credentials into a saml specific section instead of clobbering
# the default credentials
if not config.has_section(sectionName):
    config.add_section(sectionName)

config.set(sectionName, 'output', outputformat)
config.set(sectionName, 'region', region)
config.set(sectionName, 'aws_access_key_id', token['Credentials']['AccessKeyId'])
config.set(sectionName, 'aws_secret_access_key', token['Credentials']['SecretAccessKey'])
config.set(sectionName, 'aws_session_token', token['Credentials']['SessionToken'])

# Write the updated config file
with open(filename, 'w+') as configfile:
    config.write(configfile)

# Give the user some basic info as to what has just happened
print('\n\n----------------------------------------------------------------')
print('Your new access key pair has been stored in the AWS configuration file {0} under the saml profile.'.format(filename))
print('Note that it will expire at {0}.'.format(token['Credentials']['Expiration']))
print('After this time, you may safely rerun this script to refresh your access key pair.')
print('To use this credential, call the AWS CLI with the --profile option (e.g. aws --profile saml ec2 describe-instances).')
print('----------------------------------------------------------------\n\n')