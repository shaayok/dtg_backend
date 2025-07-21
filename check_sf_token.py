import requests

# Function to get Salesforce access token using password grant type
def get_salesforce_access_token(
    client_id,
    client_secret,
    username,
    password,
    security_token,
    token_url='https://test.salesforce.com/services/oauth2/token',
    existing_token=None,
    instance_url=None
):
    """
    Returns a valid Salesforce access token. If existing_token is valid, returns it. Otherwise, fetches a new one.
    """
    # If an existing token and instance_url are provided, validate it
    if existing_token and instance_url:
        if is_token_valid(existing_token, instance_url):
            return existing_token, instance_url
    
    # Combine password and security token
    full_password = password + security_token
    payload = {
        'grant_type': 'password',
        'client_id': client_id,
        'client_secret': client_secret,
        'username': username,
        'password': full_password
    }
    response = requests.post(token_url, data=payload)
    if response.status_code == 200:
        token_data = response.json()
        return token_data['access_token'], token_data['instance_url']
    else:
        raise Exception(f"Failed to get access token: {response.status_code} {response.text}")

def is_token_valid(access_token, instance_url):
    """
    Checks if the provided Salesforce access token is valid by making a simple API call.
    """
    url = f"{instance_url}/services/data/v60.0/"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    response = requests.get(url, headers=headers)
    return response.status_code == 200
