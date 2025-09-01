import requests
import json
from check_sf_token import get_salesforce_access_token
import os
from dotenv import load_dotenv
load_dotenv()

def contact_create_update(request_body):
    # ---- Helpers ----
    def query_soql(soql):
        url = f"{instance_url}/services/data/v61.0/query"
        resp = requests.get(url, headers=headers, params={"q": soql})
        resp.raise_for_status()
        return resp.json()

    def create_record(object_name, data):
        url = f"{instance_url}/services/data/v61.0/sobjects/{object_name}/"
        resp = requests.post(url, headers=headers, json=data)
        if not resp.ok:
            print("Salesforce Error:", resp.text)
            resp.raise_for_status()
        return resp.json()["id"]

    def delete_record(object_name, record_id):
        url = f"{instance_url}/services/data/v61.0/sobjects/{object_name}/{record_id}"
        resp = requests.delete(url, headers=headers)
        if resp.status_code != 204:
            print("Delete failed:", resp.text)
    
    def update_contact(contact_id, updates):
        url = f"{instance_url}/services/data/v61.0/sobjects/Contact/{contact_id}"
        resp = requests.patch(url, headers=headers, json=updates)
        if not resp.ok:
            print("Salesforce Error (update contact):", resp.text)
            resp.raise_for_status()
        else:
            print(f"Updated Contact {contact_id} with {updates}")


    # ---- Auth ----
    access_token, instance_url = get_salesforce_access_token(
        client_id=os.getenv('SALESFORCE_CLIENT_ID'),
        client_secret=os.getenv('SALESFORCE_CLIENT_SECRET'),
        username=os.getenv('SALESFORCE_USERNAME'),
        password=os.getenv('SALESFORCE_PASSWORD'),
        security_token=os.getenv('SALESFORCE_SECURITY_TOKEN')
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # ---- Input ----
    data = request_body
    email = data.get("email")
    first_name = data.get("firstName")
    last_name = data.get("lastName")
    primary_account_name = data.get("amazonSite")
    managed_accounts = data.get("managedAccounts", [])
    type_req = data.get("type", "setup")

    if type_req == "setup":
        incoming_account_names = [primary_account_name] + managed_accounts
    else:
        incoming_account_names = primary_account_name.split(',')
    # ---- Step 1: Resolve Accounts (must exist) ----
    account_name_to_id = {}
    for name in incoming_account_names:
        if not name:
            continue
        accs = query_soql(f"SELECT Id FROM Account WHERE Name = '{name.strip()}'")
        if accs["totalSize"] == 0:
            raise ValueError(f"Account not found: {name}")
        account_name_to_id[name] = accs["records"][0]["Id"]

    # ---- Step 2: Get or Create Contact ----
    cts = query_soql(f"SELECT Id, AccountId FROM Contact WHERE Email = '{email}'")
    if cts["totalSize"] == 0:
        # New Contact
        contact_data = {
            "FirstName": first_name,
            "LastName": last_name,
            "Email": email,
            "AccountId": account_name_to_id[primary_account_name],
            "Department__c": "Sales",
            "Status__c": "Prospect",
            "Contact_Type__c": "End User",
            "LeadSource": "Website",
            "OwnerId": "0051I000001qk6a"
        }
        contact_id = create_record("Contact", contact_data)
        current_account_id = account_name_to_id[primary_account_name]
    else:
        # Existing Contact
        contact_id = cts["records"][0]["Id"]
        current_account_id = cts["records"][0]["AccountId"]

    if type_req != "setup":
        update_contact(contact_id, {
        "FirstName": first_name,
        "LastName": last_name
    })
    # ---- Step 3: Fetch existing ACRs ----
    acrs = query_soql(f"""
        SELECT Id, AccountId, Account.Name 
        FROM AccountContactRelation 
        WHERE ContactId = '{contact_id}'
    """)
    existing_acrs = {rec["AccountId"]: rec["Id"] for rec in acrs["records"]}

    # Include current AccountId
    if current_account_id:
        existing_acrs[current_account_id] = None  # None = owned via Contact.AccountId

    # ---- Step 4: Compute adds/removes ----
    incoming_ids = set(account_name_to_id.values())
    existing_ids = set(existing_acrs.keys())

    to_add = incoming_ids - existing_ids
    to_remove = existing_ids - incoming_ids

    # ---- Step 5: Add new ACRs ----
    for acc_id in to_add:
        try:
            create_record("AccountContactRelation", {
                "AccountId": acc_id,
                "ContactId": contact_id
            })
            print(f"Linked {email} to Account {acc_id}")
        except Exception as e:
            print(f"Failed linking {email} to {acc_id}: {e}")

    # ---- Step 6: Remove missing ACRs (skip primary AccountId) ----
    for acc_id in to_remove:
        acr_id = existing_acrs[acc_id]
        if acr_id:  # skip primary (AccountId)
            delete_record("AccountContactRelation", acr_id)
            print(f"Removed link between {email} and Account {acc_id}")

    print(
    f"Contact {first_name} {last_name} ({email}) synced with accounts: {incoming_account_names}. "
    f"Link: {instance_url}/{contact_id}"
    )
