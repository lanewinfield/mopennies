'''
Authentication
'''

import getpass
import logging
import os
import os.path
import re
import shutil
import xml.etree.ElementTree as ET

# Python 2.x fixes
try: import configparser
except ImportError:
    import ConfigParser as configparser

try: from urllib.parse import urlencode
except ImportError:
    from urllib import urlencode

try: input = raw_input
except NameError: pass

import spammo

logger = logging.getLogger('spammo.auth')


def configure():
    '''Get venmo ready to make payments.

    First, set username and password. If those change, then get access token.
    If that fails, unset username and password.

    Return whether or not we save an access token.
    '''
    # Update credentials
    credentials = update_credentials()
    if not credentials:
        return False
    else:
        email, password = credentials

    # Log in to Auto
    success = submit_credentials(email, password)
    if not success:
        return False
    else:
        redirect_url, auth_request, csrftoken2 = success

    # 2FA is expected because issue #23
    if 'two-factor' not in redirect_url:
        logger.error('invalid credentials')
        return False

    # Write email password
    config = read_config()
    config.set(configparser.DEFAULTSECT, 'email', email)
    config.set(configparser.DEFAULTSECT, 'password', password)
    write_config(config)

    # Do 2FA
    access_token = two_factor(redirect_url, auth_request, csrftoken2)
    if not access_token:
        return False

    # Write access token
    config = read_config()
    config.set(configparser.DEFAULTSECT, 'access_token', access_token)
    write_config(config)
    return True


def two_factor(redirect_url, auth_request, csrftoken2):
    '''Do the two factor auth dance.

    Return access_token or False.
    '''
    logger.info('Sending SMS verification ...')

    # Get two factor page
    response = spammo.singletons.session().get(redirect_url)

    # Send SMS
    secret = extract_otp_secret(response.text)
    headers = {'Venmo-Otp-Secret': secret}
    data = {
        'via': 'sms',
        'csrftoken2': csrftoken2,
    }
    response = spammo.singletons.session().post(
        spammo.settings.TWO_FACTOR_URL,
        json=data,
        headers=headers,
    )
    assert response.status_code == 200, 'Post to 2FA failed'
    assert response.json()['data']['status'] == 'sent', 'SMS did not send'

    # Prompt verification code
    verification_code = input('Verification code: ')
    if not verification_code:
        logger.error('verification code required')
        return False

    # Submit verification code
    headers['Venmo-Otp'] = verification_code
    data = {
        'auth_request': auth_request,
        'csrftoken2': csrftoken2,
    }
    response = spammo.singletons.session().post(
        spammo.settings.TWO_FACTOR_AUTHORIZATION_URL,
        headers=headers,
        json=data,
        allow_redirects=False,
    )
    if response.status_code != 200:
        logger.error('verification code failed')
        return False

    # Retrieve access token
    location = response.json()['location']
    code = location.split('code=')[1]
    access_token = retrieve_access_token(code)
    return access_token


def extract_otp_secret(text):
    pattern = re.compile('"secret":"(\w*)"')
    for line in text.splitlines():
        match = pattern.search(line)
        return match.group(1)
    raise Exception('msg="Could not extract data-otp-secret"')


def retrieve_access_token(code):
    data = {
        'client_id': spammo.settings.CLIENT_ID,
        'client_secret': spammo.settings.CLIENT_SECRET,
        'code': code,
    }
    response = spammo.singletons.session().post(spammo.settings.ACCESS_TOKEN_URL,
                                               data)
    response_dict = response.json()
    access_token = response_dict['access_token']
    return access_token


def _authorization_url():
    scopes = [
        'make_payments',
        'access_feed',
        'access_profile',
        'access_email',
        'access_phone',
        'access_balance',
        'access_friends',
    ]
    params = {
        'client_id': spammo.settings.CLIENT_ID,
        'scope': ' '.join(scopes),
        'response_type': 'code',
    }
    return '{authorization_url}?{params}'.format(
        authorization_url=spammo.settings.AUTHORIZATION_URL,
        params=urlencode(params)
    )


def _filter_tag(input_xml, tag):
    '''Filter out the script so we can parse the xml.'''
    output_lines = []
    inside = False
    for line in input_xml.splitlines():
        if '<{tag}>'.format(**locals()) in line:
            inside = True
        if not inside:
            output_lines.append(line)
        if '</{tag}>'.format(**locals()) in line:
            inside = False
    return '\n'.join(output_lines)


def update_credentials():
    '''Save username and password to config file.

    Entering nothing keeps the current credentials. Returns whether or not
    the credentials changed.
    '''
    # Read old credentials
    config = read_config()
    try:
        old_email = config.get(configparser.DEFAULTSECT, 'email')
    except configparser.NoOptionError:
        old_email = ''
    try:
        old_password = config.get(configparser.DEFAULTSECT, 'password')
    except configparser.NoOptionError:
        old_password = ''

    # Prompt new credentials
    email = input('Venmo email [{}]: '
                      .format(old_email if old_email else None))
    password = getpass.getpass(prompt='Venmo password [{}]: '
                               .format('*' * 10 if old_password else None))
    email = email or old_email
    password = password or old_password

    incomplete = not email or not password
    if incomplete:
        logger.warn('credentials incomplete')
        return False

    return email, password


def submit_credentials(email, password):
    # Get and parse authorization webpage xml and form
    response = spammo.singletons.session().get(_authorization_url())
    authorization_page_xml = response.text
    filtered_xml = _filter_tag(authorization_page_xml, 'script')
    filtered_xml = _filter_tag(filtered_xml, 'head')
    root = ET.fromstring(filtered_xml)
    form = root.find('.//form')
    for child in form:
        if child.attrib.get('name') == 'csrftoken2':
            csrftoken2 = child.attrib['value']
        if child.attrib.get('name') == 'auth_request':
            auth_request = child.attrib['value']
        if child.attrib.get('name') == 'web_redirect_url':
            web_redirect_url = child.attrib['value']

    # Submit form
    data = {
        'username': email,
        'password': password,
        'web_redirect_url': web_redirect_url,
        'csrftoken2': csrftoken2,
        'auth_request': auth_request,
        'grant': 1,
    }
    response = spammo.singletons.session().post(
        spammo.settings.AUTHORIZATION_URL,
        json=data,
        allow_redirects=False)
    if response.status_code != 302:
        logger.error('expecting a redirect')
        return False

    redirect_url = response.headers['location']
    return redirect_url, auth_request, csrftoken2


def get_username():
    config = read_config()
    try:
        return config.get(configparser.DEFAULTSECT, 'email')
    except configparser.NoOptionError:
        return None


def get_password():
    config = read_config()
    return config.get(configparser.DEFAULTSECT, 'password')


def get_access_token():
    config = read_config()
    try:
        return config.get(configparser.DEFAULTSECT, 'access_token')
    except configparser.NoOptionError:
        return None


def read_config():
    config = configparser.RawConfigParser()
    config.read(spammo.settings.CREDENTIALS_FILE)
    return config


def write_config(config):
    try:
        os.makedirs(os.path.dirname(spammo.settings.CREDENTIALS_FILE))
    except OSError:
        pass  # It's okay if directory already exists
    with open(spammo.settings.CREDENTIALS_FILE, 'w') as configfile:
        config.write(configfile)


def reset():
    '''rm -rf ~/.venmo'''
    shutil.rmtree(spammo.settings.DOT_VENMO)
