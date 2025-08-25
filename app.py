from flask import request, jsonify
from flask_cors import CORS
from flask import Flask
import requests
import urllib.parse
from check_sf_token import get_salesforce_access_token
from datetime import datetime
import threading
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import re
import os
from dotenv import load_dotenv
from pdf_creator_1 import send_test_email_with_pdf
load_dotenv()   



app = Flask(__name__)
CORS(app)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'supersecretkey')

@app.route('/', methods=['GET'])
def welcome():
    return jsonify({"message": "Welcome to DTG Backend", "status": "active"}), 200

@app.route('/api', methods=['GET'])
def healthcheck():
    page = request.args.get('page', 1)
    limit = request.args.get('limit', 10)
    return jsonify({"status": "ok",
                    "page": page,
                    "limit": limit}), 200


@app.route('/api/quote', methods=['POST'])
def quote():
    #Salesforce Auth

    access_token, instance_url = get_salesforce_access_token(
        client_id=os.getenv('SALESFORCE_CLIENT_ID'),
        client_secret=os.getenv('SALESFORCE_CLIENT_SECRET'),
        username=os.getenv('SALESFORCE_USERNAME'),
        password=os.getenv('SALESFORCE_PASSWORD'),
        security_token=os.getenv('SALESFORCE_SECURITY_TOKEN')
    )
    print('Generated Access Token and Instance URL')
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    data = request.json
    #print("Incoming quote request:", data)
    
    if not data:
        return jsonify({"status": False, "error": "Missing or invalid JSON body"}), 400
    customer = data.get('customer', {})
    user = data.get('user', {})
    items = data.get('items', [])
    change = data.get('addressChange', "N")

    # Extract user & account info
    account_name = user.get('customFields', {}).get('amazon-site', 'Unnamed Account')

    # Combine shipping address
    shipping_address = {
        "street": f"{customer.get('address1', '')} {customer.get('address2', '')}".strip(),
        "city": customer.get("city", ""),
        "state": customer.get("state", ""),
        "postal_code": customer.get("zip", ""),
        "country": customer.get("country", "")
    }

    # Normalize items into products array
    products = []
    for item in items:
        products.append({
            "partnumber": item.get("partnumber"),
            "description": item.get("description", ""),
            "qty": int(item.get("qty", 1))
        })

    # Final transformed payload
    quote_payload = {
        "account_name": account_name,
        "shipping_address": shipping_address,
        "products": products
    }

    print(quote_payload)
    print("\n------------------------------------------\n")

    account_defaults = {
        "Account_Type__c": "Customer",
        "Customer_Type__c": "End User",
        "Status__c": "Active",
        "Industry": "Warehouse Logistics",
        "Potential__c": "High",
        "ParentId": "0011I00000MtQYxQAN",
        "AccountSource": "Website",
        "Type": "Customer"
    }

    def get_account(account_name):
        soql = f"SELECT Id, ShippingStreet, ShippingCity, ShippingState, ShippingPostalCode, ShippingCountry FROM Account WHERE Name = '{account_name}' LIMIT 1"
        url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(soql)}"
        r = requests.get(url, headers=headers)
        records = r.json().get('records', [])
        return records[0] if records else None

    def create_account(name, shipping, defaults):
        payload = {
            "Name": name,
            "ShippingStreet": shipping["street"],
            "ShippingCity": shipping["city"],
            "ShippingState": shipping["state"],
            "ShippingPostalCode": shipping["postal_code"],
            "ShippingCountry": 'US',
            **defaults
        }
        url = f"{instance_url}/services/data/v60.0/sobjects/Account/"
        r = requests.post(url, headers=headers, json=payload)
        if r.status_code == 201:
            return r.json()['id']
        print("Account creation failed:", r.text)
        return None

    def update_account_address(account_id, shipping):
        url = f"{instance_url}/services/data/v60.0/sobjects/Account/{account_id}"
        payload = {
            "ShippingStreet": shipping["street"],
            "ShippingCity": shipping["city"],
            "ShippingState": shipping["state"],
            "ShippingPostalCode": shipping["postal_code"],
            "ShippingCountry": 'US'
        }
        r = requests.patch(url, headers=headers, json=payload)
        return r.status_code == 204

    def address_differs(existing, shipping):
        return (
            existing.get("ShippingStreet", "").strip().lower() != shipping["street"].lower().strip() or
            existing.get("ShippingCity", "").strip().lower() != shipping["city"].lower().strip() or
            existing.get("ShippingState", "").strip().lower() != shipping["state"].lower().strip() or
            existing.get("ShippingPostalCode", "").strip() != shipping["postal_code"].strip()
        )

    def create_sales_order(account_id):
        url = f"{instance_url}/services/data/v60.0/sobjects/gii__SalesOrder__c/"
        payload = {
            "gii__Account__c": account_id,
            "gii__OrderType__c": "Standard",
            "gii__PaymentTerms__c": "Net 30",
            "Sales_Order_Name__c": "DEMO Test Order",
            "Sales_Type__c": "Demo"
        }
        r = requests.post(url, headers=headers, json=payload)
        if r.status_code == 201:
            return r.json()['id']
        print("Sales order creation failed:", r.text)
        return None

    def get_product_id(partnumber):
        soql = f"SELECT Id FROM gii__Product2Add__c WHERE Name = '{partnumber}' LIMIT 1"
        url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(soql)}"
        r = requests.get(url, headers=headers)
        records = r.json().get('records', [])
        return records[0]['Id'] if records else None

    def create_sales_order_line(order_id, product_id, description, qty):
        url = f"{instance_url}/services/data/v60.0/sobjects/gii__SalesOrderLine__c/"
        payload = {
            "gii__SalesOrder__c": order_id,
            "gii__Product__c": product_id,
            "gii__OrderQuantity__c": qty,
            "gii__StockUM__c": "Each"
        }
        r = requests.post(url, headers=headers, json=payload)
        if r.status_code == 201:
            print(f"✅ Created line for {product_id} (Qty: {qty})")
        else:
            print(f"❌ Failed to create line for {product_id}: {r.text}")
    
    def create_sales_quote(account_id):
        email = user.get('auth', {}).get('email', '')
        now = datetime.now()
        unique_key = f"{email}_{now.strftime('%Y%m%d%H%M%S%f')}"
        url = f"{instance_url}/services/data/v60.0/sobjects/gii__SalesQuote__c/"
        payload = {
            "gii__Account__c": account_id,
            "Quote_Name__c": f"Test Quote on {now.strftime('%d %B %Y %H:%M')}",
            "gii__Status__c": "Open",
            "gii__SalesRepresentative__c": "0031I000009dExWQAU",
            "OwnerId": "0051I000001qk6a",
            "Portal_Request__c": unique_key
        }
        r = requests.post(url, headers=headers, json=payload)
        if r.status_code == 201:
            print("✅ Created Sales Quote")
            print(r.json())
            return (r.json()['id'], unique_key)
        else:
            print("❌ Failed to create Sales Quote:", r.text)
            return None
        
    def get_sales_quotes_name(quote_id):
        query = f"""
        SELECT Id, Name, gii__Status__c, gii__QuoteDate__c 
        FROM gii__SalesQuote__c 
        WHERE Id = '{quote_id}'
        """     
        url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(query)}"
        r = requests.get(url, headers=headers)
        return r.json().get("records", [])

    def create_sales_quote_line(sales_quote_id, product_id, quantity):
        url = f"{instance_url}/services/data/v60.0/sobjects/gii__SalesQuoteLine__c/"
        payload = {
            "gii__SalesQuote__c": sales_quote_id,
            "gii__Product__c": product_id,
            "gii__OrderQuantity__c": quantity
        }
        r = requests.post(url, headers=headers, json=payload)
        if r.status_code == 201:
            print(f"✅ Created quote line for product {product_id}")
        else:
            print(f"❌ Failed to create quote line for product {product_id}: {r.text}")


    # === MAIN FLOW ===
    account = get_account(account_name)
    if account:
        account_id = account['Id']
        print("✅ Account found:", account_id)
        if len(shipping_address["street"]) > 5 and len(shipping_address["city"]) >= 2 and len(shipping_address["state"]) >= 2 and len(shipping_address["postal_code"]) > 2:
            print("✅ Shipping address entered is valid")
            if address_differs(account, shipping_address):
                updated = update_account_address(account_id, shipping_address)
                change = "Y" if updated else "N"
                print("🔄 Address updated" if updated else "⚠️ Address update failed")
            else:
                print("✅ Account found and address is up-to-date")
        else:
            print("⚠️ Shipping address is incomplete, skipping address update")
    else:
        account_id = create_account(account_name, shipping_address, account_defaults)
        if not account_id:
            raise Exception("Failed to create account")
    
    sales_quote_id, portal_key = create_sales_quote(account_id)
    if not sales_quote_id:
        raise Exception("Cannot proceed without a sales quote.")

    # Step 2: Loop through products and create quote lines
    for product in products:
        product_id = get_product_id(product["partnumber"])
        if not product_id:
            print(f"❌ Product not found: {product['partnumber']}")
            continue
        create_sales_quote_line(
            sales_quote_id=sales_quote_id,
            product_id=product_id,
            quantity=product["qty"]
        )


    print("✅ Sales Quote and Lines created successfully")
    link = f"{instance_url}/lightning/r/gii__SalesQuote__c/{sales_quote_id}/view"
    print("🔗 View Sales Quote:", link)
    

    def send_email_async(payload):
        def _go():
            try:
                # Update with your email API endpoint (can be localhost or ngrok URL)
                requests.post("http://localhost:5000/api/send-quote-email", json=payload, timeout=10)
            except Exception as e:
                print("Background email failed:", e)
        threading.Thread(target=_go, daemon=True).start()
    
    def send_email_pdf(payload):
        def _go():
            try:
                # Update with your email API endpoint (can be localhost or ngrok URL)
                requests.post("http://localhost:5000/api/send-pdf-email", json=payload, timeout=10)
            except Exception as e:
                print("Background email failed:", e)
        threading.Thread(target=_go, daemon=True).start()

    # At the end of quote() BEFORE return:
    quote_info = get_sales_quotes_name(sales_quote_id)
    send_email_async({
        "link": link,  # Salesforce quote link
        "created_by_email": user.get('auth', {}).get('email', ''),
        "first_name": user.get('customFields', {}).get('first-name', ''),
        "last_name": user.get('customFields', {}).get('last-name', ''),
        "account_name": account_name,
        "address_changed": change,
        "shipping_address": shipping_address,
        "products": products,
        "name": quote_info[0].get("Name") if quote_info else "Unknown",
        "portal_request": portal_key
    })

    send_email_pdf({"created_by_email": user.get('auth', {}).get('email', ''),
                              "quote_id": sales_quote_id,
                              "account_name": account_name,
                              "shipping_address": shipping_address,
                              "first_name": user.get('customFields', {}).get('first-name', '')})

    # # Create Sales Order
    # sales_order_id = create_sales_order(account_id)
    # if not sales_order_id:
    #     raise Exception("Failed to create sales order")

    # # Create Sales Order Lines
    # for product in products:
    #     product_id = get_product_id(product["partnumber"])
    #     if not product_id:
    #         print(f"❌ Product not found: {product['partnumber']}")
    #         continue
    #     create_sales_order_line(sales_order_id, product_id, product["description"], product["qty"])
    return jsonify({"status": True,
                   "message": "Sales Quote created successfully",
                    "link": link}), 200

@app.route('/api/send-pdf-email', methods=['POST'])
def send_pdf_email():
    def get_sales_quote_lines(quote_id):
        query = f"SELECT Id, gii__Product__c, gii__OrderQuantity__c FROM gii__SalesQuoteLine__c WHERE gii__SalesQuote__c = '{quote_id}'"
        url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(query)}"
        r = requests.get(url, headers=headers)
        return r.json().get("records", [])
    
    def get_product_details(product_id):
        query = f"SELECT Name, Amazon_Price__c, gii__Description__c FROM gii__Product2Add__c WHERE Id = '{product_id}' LIMIT 1"
        url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(query)}"
        r = requests.get(url, headers=headers)
        records = r.json().get("records", [])
        if records:
            return records[0]["Name"], records[0].get("Amazon_Price__c", "N/A"), records[0].get("gii__Description__c", "N/A")
        return "Unknown", "N/A", "N/A"
    
    def get_sales_quotes_name(quote_id):
        query = f"""
        SELECT Id, Name, gii__Status__c, gii__QuoteDate__c 
        FROM gii__SalesQuote__c 
        WHERE Id = '{quote_id}'
        """     
        url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(query)}"
        r = requests.get(url, headers=headers)
        return r.json().get("records", [])
    def format_shipping_address(addr: dict) -> str:
        """Safely combine address parts into a single string with newlines."""
        parts = [
            addr.get("street", ""),
            " ".join([addr.get("city",""), addr.get("state",""), addr.get("postal_code","")]).strip(),
            addr.get("country", "")
        ]
        return "\n".join([p for p in parts if p])   # remove empty parts

    
    access_token, instance_url = get_salesforce_access_token(
        client_id=os.getenv('SALESFORCE_CLIENT_ID'),
        client_secret=os.getenv('SALESFORCE_CLIENT_SECRET'),
        username=os.getenv('SALESFORCE_USERNAME'),
        password=os.getenv('SALESFORCE_PASSWORD'),
        security_token=os.getenv('SALESFORCE_SECURITY_TOKEN')
    )
    #print('Generated Access Token and Instance URL')
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    data = request.get_json()
    quote_id = data.get("quote_id")
    quote_info = get_sales_quotes_name(quote_id)
    quote_data = {
                    "name": quote_info[0]['Name'],
                    "status": quote_info[0]['gii__Status__c'],
                    "shipping_address":format_shipping_address(data.get("shipping_address")),
                    "account_name": data.get("account_name"),
                    "creator": data.get("created_by_email", ""),
                    "first_name": data.get("first_name", ""),
                    "lines": []
                }
    quote_lines = get_sales_quote_lines(quote_id)
    for ql in quote_lines:
        pname, pprice, pdescription = get_product_details(ql["gii__Product__c"])
        # Convert price to float, handle "N/A" case
        try:
            price_float = float(pprice) if pprice != "N/A" else 0.0
        except (ValueError, TypeError):
            price_float = 0.0
            
        line_data = {
            "name": pname,
            "qty": ql['gii__OrderQuantity__c'],
            "price": price_float,
            "description": pdescription
        }
        quote_data["lines"].append(line_data)
    print("Quote data prepared for PDF:", quote_data)    
    send_test_email_with_pdf(quote_data)
    return jsonify(quote_data)
    


@app.route('/api/fetch-address', methods=['POST'])
def fetch_address():
    def split_address_by_words(street):
        words = street.split()
        n = len(words)
        if n == 0:
            return "", ""
        half = (n + 1) // 2  # first half gets the extra word if odd
        address1 = " ".join(words[:half])
        address2 = " ".join(words[half:]) if n > half else ""
        return address1, address2
    data = request.json
    if not data:
        return jsonify({"status": False, "error": "Missing or invalid JSON body"}), 400
    account_name = data.get('account_name')
    first_name = data.get('first_name'," ")
    last_name = data.get('last_name'," ")
    # Connect to Salesforce and fetch address (pseudo-code below)
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
    soql = f"SELECT Id, ShippingStreet, ShippingCity, ShippingState, ShippingPostalCode, ShippingCountry FROM Account WHERE Name = '{account_name}' LIMIT 1"
    url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(soql)}"
    r = requests.get(url, headers=headers)
    records = r.json().get('records',[])
    address = records[0] if records else None
    if not address:
        return jsonify({"status": False, "error": "Address not found for the given account"}), 404
    shipping_street = address.get('ShippingStreet', '')
    if shipping_street:
        address1, address2 = split_address_by_words(shipping_street)
    else:
        address1, address2 = "", ""    

    address_resp = {
        "shipto": first_name + " " + last_name,
        "address1": address1,
        "address2": address2,
        "city": address.get('ShippingCity'),
        "state": address.get('ShippingState'),
        "zip": address.get('ShippingPostalCode'),
        "country": address.get('ShippingCountry')
    }
    return jsonify(address_resp)



@app.route('/api/send-quote-email', methods=['POST'])
def send_quote_email():
    data = request.json
    if not data:
        return jsonify({"status": False, "error": "Missing or invalid JSON body"}), 400

    # Email config
    gmail_user = os.getenv('GMAIL_USER')
    gmail_app_password = os.getenv('GMAIL_APP_PASSWORD')
    
    # Get both email recipients
    email_to_1 = os.getenv('EMAIL_TO')
    email_to_2 = 'sayaksamaddar@virtualemployee.com'  # Second email recipient
    email_to_3 = 'amazon-portal-activit-aaaaq74u3hzgbxwefmrhystcaa@the-dtg.slack.com'
    
    # Create list of recipients (filter out empty emails)
    recipients = [email for email in [email_to_1, email_to_2, email_to_3] if email.strip()]
    to_email = ', '.join(recipients)

    # Email subject
    subject = f"New Sales Quote: {data.get('account_name', 'Unknown Account')}"

    # Pretty HTML template for quote summary
    html = f"""
    <html>
    <body>
        <h2>New Sales Quote Created {data.get('name', 'Unknown Quote')}</h2>
        <p><b>Account:</b> {data.get('account_name','')}</p>
        <p><b>Created by:</b> {data.get('first_name','')} {data.get('last_name','')} ({data.get('created_by_email','')})</p>
        <p><b>Address Changed?</b> {"Yes" if data.get('address_changed') == "Y" else "No"}</p>
        <p><b>Portal Request Id</b> {data.get('portal_request', '')}</p>
        <p><b>Shipping Address:</b><br>
            {data.get('shipping_address', {}).get('street', '')}<br>
            {data.get('shipping_address', {}).get('city', '')}, {data.get('shipping_address', {}).get('state', '')} {data.get('shipping_address', {}).get('postal_code', '')}
        </p>
        <p>
            <a href="{data.get('link','')}" style="background:#4CAF50;color:white;padding:8px 15px;text-decoration:none;border-radius:6px;">View Quote in Salesforce</a>
        </p>
        <h3>Products:</h3>
        <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;">
            <tr>
                <th>Part #</th><th>Description</th><th>Qty</th>
            </tr>
            {''.join([
                f"<tr><td>{p.get('partnumber')}</td><td>{p.get('description')}</td><td>{p.get('qty')}</td></tr>"
                for p in data.get('products', [])
            ])}
        </table>
        <br>
        <p style="color:#888;">This is an automated notification.</p>
    </body>
    </html>
    """

    # Create email message
    msg = MIMEMultipart("alternative")
    msg['Subject'] = subject
    msg['From'] = gmail_user
    msg['To'] = to_email

    msg.attach(MIMEText(html, 'html'))

    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(gmail_user, gmail_app_password)
            server.send_message(msg)
        print('Email sent!')
        return jsonify({"status": True, "message": "Email sent!"})
    except Exception as e:
        print(f'Email error: {e}')
        return jsonify({"status": False, "error": str(e)}), 500

@app.route('/api/account-data', methods=['GET'])
def get_account_data():
    access_token, instance_url = get_salesforce_access_token(
        client_id=os.getenv('SALESFORCE_CLIENT_ID'),
        client_secret=os.getenv('SALESFORCE_CLIENT_SECRET'),
        username=os.getenv('SALESFORCE_USERNAME'),
        password=os.getenv('SALESFORCE_PASSWORD'),
        security_token=os.getenv('SALESFORCE_SECURITY_TOKEN')
    )
    print('Generated Access Token and Instance URL')
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    def get_account_id(account_name):
        query = f"SELECT Id FROM Account WHERE Name = '{account_name}' LIMIT 1"
        url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(query)}"
        r = requests.get(url, headers=headers)
        print(r.json())
        records = r.json().get("records", [])
        return records[0]["Id"] if records else None

    def get_product_details(product_id):
        query = f"SELECT Name, Amazon_Price__c, gii__Description__c FROM gii__Product2Add__c WHERE Id = '{product_id}' LIMIT 1"
        url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(query)}"
        r = requests.get(url, headers=headers)
        records = r.json().get("records", [])
        if records:
            return records[0]["Name"], records[0].get("Amazon_Price__c", "N/A"), records[0].get("gii__Description__c", "N/A")
        return "Unknown", "N/A", "N/A"

    def get_sales_orders(account_id, page=1):
        offset = (page - 1) * 5
        query = f"""
        SELECT Id, Name, gii__Status__c, gii__OrderType__c, gii__OrderStatus__c, 
            gii__SalesQuote__c, gii__SalesQuote__r.Quote_Name__c, gii__OrderDate__c
        FROM gii__SalesOrder__c 
        WHERE gii__Account__c = '{account_id}'
        ORDER BY gii__OrderDate__c DESC
        LIMIT 5 OFFSET {offset}
        """
        url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(query)}"
        r = requests.get(url, headers=headers)
        return r.json().get("records", [])

    def get_sales_order_lines(sales_order_id):
        query = f"SELECT Id, gii__Product__c, gii__OrderQuantity__c FROM gii__SalesOrderLine__c WHERE gii__SalesOrder__c = '{sales_order_id}'"
        url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(query)}"
        r = requests.get(url, headers=headers)
        return r.json().get("records", [])

    def get_sales_quotes(account_id, page=1):
        offset = (page - 1) * 5
        query = f"""
        SELECT Id, Name, gii__Status__c, gii__QuoteDate__c 
        FROM gii__SalesQuote__c 
        WHERE gii__Account__c = '{account_id}'
        ORDER BY gii__QuoteDate__c DESC
        LIMIT 5 OFFSET {offset}
        """
        url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(query)}"
        r = requests.get(url, headers=headers)
        return r.json().get("records", [])

    def get_shipments_for_order(sales_order_id):
        """
        Returns a list of dicts with gii__TrackingLink__c and gii__ShipmentStatus__c
        for all shipments related to a sales order.
        """
        query = (
            "SELECT Id, gii__TrackingLink__c, gii__ShipmentStatus__c "
            f"FROM gii__Shipment__c WHERE gii__SalesOrder__c = '{sales_order_id}'"
        )
        url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(query)}"
        r = requests.get(url, headers=headers)
        if r.status_code != 200:
            return []
        records = r.json().get("records", [])
        shipments = [
            {
                "tracking_link": rec.get("gii__TrackingLink__c"),
                "shipment_status": rec.get("gii__ShipmentStatus__c"),
            }
            for rec in records
        ]
        return shipments


    def get_sales_quote_lines(quote_id):
        query = f"SELECT Id, gii__Product__c, gii__OrderQuantity__c FROM gii__SalesQuoteLine__c WHERE gii__SalesQuote__c = '{quote_id}'"
        url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(query)}"
        r = requests.get(url, headers=headers)
        return r.json().get("records", [])
    
    def get_order_stats(account_id):
        """Returns total orders and open orders count for an account"""
        # Total orders
        total_query = f"SELECT COUNT() FROM gii__SalesOrder__c WHERE gii__Account__c = '{account_id}'"
        total_url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(total_query)}"
        total_response = requests.get(total_url, headers=headers)
        total_orders = total_response.json().get("totalSize", 0)
        
        # Open orders
        open_query = f"SELECT COUNT() FROM gii__SalesOrder__c WHERE gii__Account__c = '{account_id}' AND gii__Status__c = 'Open'"
        open_url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(open_query)}"
        open_response = requests.get(open_url, headers=headers)
        open_orders = open_response.json().get("totalSize", 0)
        
        return total_orders, open_orders
    
    def get_quote_stats(account_id):
        """Returns total quotes and open quotes count for an account"""
        # Total quotes
        total_query = f"SELECT COUNT() FROM gii__SalesQuote__c WHERE gii__Account__c = '{account_id}'"
        total_url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(total_query)}"
        total_response = requests.get(total_url, headers=headers)
        total_quotes = total_response.json().get("totalSize", 0)
        
        # Open quotes
        open_query = f"SELECT COUNT() FROM gii__SalesQuote__c WHERE gii__Account__c = '{account_id}' AND gii__Status__c = 'Open'"
        open_url = f"{instance_url}/services/data/v60.0/query?q={urllib.parse.quote(open_query)}"
        open_response = requests.get(open_url, headers=headers)
        open_quotes = open_response.json().get("totalSize", 0)
        
        return total_quotes, open_quotes
    
    # Get account name from query parameter
    account_name = request.args.get('account_name')
    tab = request.args.get('type', 'orders')
    
    if not account_name:
        return jsonify({"error": "account_name parameter is required"}), 400
    
    # Get page parameter (default to 1)
    try:
        page = int(request.args.get('page', 1))
        if page < 1:
            page = 1
    except ValueError:
        page = 1
    
    # Get account ID
    account_id = get_account_id(account_name)
    if not account_id:
        return jsonify({"error": f"Account '{account_name}' not found"}), 404
    
    # Build the result structure
    result = {
        "orders": [],
        "quotes": [],
        "page": page,
        "page_size": 5,
        "total_orders": 0,
        "total_quotes": 0,
        "open_orders": 0,
        "open_quotes": 0
    }
    
    try:
        if tab == 'orders':
            # --- SALES ORDERS ---
            orders = get_sales_orders(account_id, page)
            total_orders, open_orders = get_order_stats(account_id)
            for order in orders:
                # Get related quote information if exists
                quote_id = order.get('gii__SalesQuote__c')
                quote_name = order.get('gii__SalesQuote__r', {}).get('Name') if order.get('gii__SalesQuote__r') else None
                quote_link = f"{instance_url}/lightning/r/gii__SalesQuote__c/{quote_id}/view" if quote_id else None
                shipments = get_shipments_for_order(order["Id"])
                
                order_data = {
                    "name": order['Name'],
                    "status": order['gii__Status__c'],
                    "quote_id": quote_id,
                    "quote_name": quote_name,
                    "quote_link": quote_link,
                    "lines": []
                }
                order_data["shipments"] = shipments
                
                lines = get_sales_order_lines(order["Id"])
                for line in lines:
                    product_name, product_price, product_description = get_product_details(line["gii__Product__c"])
                    # Convert price to float, handle "N/A" case
                    try:
                        price_float = float(product_price) if product_price != "N/A" else 0.0
                    except (ValueError, TypeError):
                        price_float = 0.0
                        
                    line_data = {
                        "name": product_name,
                        "qty": line['gii__OrderQuantity__c'],
                        "price": price_float,
                        "description": product_description
                    }
                    order_data["lines"].append(line_data)
                
                result["orders"].append(order_data)
            result["total_orders"] = total_orders
            result["open_orders"] = open_orders
        else:    
            # --- SALES QUOTES ---
            quotes = get_sales_quotes(account_id, page)
            total_quotes, open_quotes = get_quote_stats(account_id)
            for quote in quotes:
                quote_data = {
                    "name": quote['Name'],
                    "status": quote['gii__Status__c'],
                    "lines": []
                }
                
                quote_lines = get_sales_quote_lines(quote["Id"])
                for ql in quote_lines:
                    pname, pprice, pdescription = get_product_details(ql["gii__Product__c"])
                    # Convert price to float, handle "N/A" case
                    try:
                        price_float = float(pprice) if pprice != "N/A" else 0.0
                    except (ValueError, TypeError):
                        price_float = 0.0
                        
                    line_data = {
                        "name": pname,
                        "qty": ql['gii__OrderQuantity__c'],
                        "price": price_float,
                        "description": pdescription
                    }
                    quote_data["lines"].append(line_data)
                
                result["quotes"].append(quote_data)
            result["total_quotes"] = total_quotes
            result["open_quotes"] = open_quotes
        return jsonify(result)
        
    except Exception as e:
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500

@app.route('/api/dashboard', methods=['GET'])
def dashboard():
    CURRENT_YEAR = datetime.now().year
    
    def get_account_data(instance_url, access_token, amazon_site_code):
    # Prepare SOQL
        fields = [
            "Battery_Blade_Connector_Count__c", "Battery_POGO_Connector_Count__c",
            "Charger_Blade_Connector_Count__c", "Charger_POGO_Connector_Count__c",
            "Controller_Blade_Connector_Count__c", "Controller_POGO_Connector_Count__c",
            "DTG_Retrofit_Kit_Count__c", "PS_Security_Cart_Count__c",
            "PS_Slam_Cart_Count__c", "PS_Cart_Count__c", "PS_Loss_Prevention_Cart_Count__c",
            "Battery_Expiration_2022__c", "Battery_Expiration_2023__c",
            "Battery_Expiration_2024__c", "Battery_Expiration_2025__c",
            "Battery_Expiration_2026__c", "Battery_Expiration_2027__c",
            "Battery_Expiration_2028__c", "Battery_Expiration_2029__c"
        ]
        field_str = ", ".join(fields)
        soql = (
            f"SELECT {field_str} FROM Account "
            f"WHERE Name = '{amazon_site_code}'"
        )
        url = f"{instance_url}/services/data/v60.0/query"
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {"q": soql}
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
        results = resp.json()
        if not results["records"]:
            raise ValueError(f"No account found for Amazon_Site_Code__c = {amazon_site_code}")
        # Get the first record (should only be one)
        account_info = {}
        account_info['product'] = results["records"][0]
        # Open orders
        open_query = (
            f"SELECT COUNT() FROM gii__SalesOrder__c "
            f"WHERE gii__Account__r.Name = '{amazon_site_code}' AND gii__Status__c = 'Open'"
        )
        open_url = f"{instance_url}/services/data/v60.0/query"
        open_response = requests.get(open_url, headers=headers, params={"q": open_query})
        open_orders = open_response.json().get("totalSize", 0)
        account_info['open_order'] = open_orders
        
        # Open quotes
        open_query = (
            f"SELECT COUNT() FROM gii__SalesQuote__c "
            f"WHERE gii__Account__r.Name = '{amazon_site_code}' AND gii__Status__c = 'Open'"
        )
        open_url = f"{instance_url}/services/data/v60.0/query"
        open_response = requests.get(open_url, headers=headers, params={"q": open_query})
        open_quotes = open_response.json().get("totalSize", 0)
        account_info['open_quote'] = open_quotes

        return account_info


    def process_account_data(account):
        # --- Product Summary ---
        product_types = {
            "Battery Blade Connector": "Battery_Blade_Connector_Count__c",
            "Battery Pogo Connector": "Battery_POGO_Connector_Count__c",
            "Charger - Blade Connector": "Charger_Blade_Connector_Count__c",
            "Charger - Pogo Connector": "Charger_POGO_Connector_Count__c",
            "Controller - Blade Connector": "Controller_Blade_Connector_Count__c",
            "Controller - Pogo Connector": "Controller_POGO_Connector_Count__c",
            "DTG Power Retrofit Kit": "DTG_Retrofit_Kit_Count__c",
            "DTG Problem Solver Security Cart": "PS_Security_Cart_Count__c",
            "DTG Slam Cart": "PS_Slam_Cart_Count__c",
            "Problem Solver Cart": "PS_Cart_Count__c",
            "Problem Solver Loss Prevention Cart": "PS_Loss_Prevention_Cart_Count__c"
        }
        product_summary = []
        for name, field in product_types.items():
            qty = account.get(field, 0) or 0
            if qty > 0:
                product_summary.append({"type": name, "quantity": qty})

        blade_expiry = []
        for year in range(2025, 2030):
            field = f"Battery_Expiration_{year}__c"
            qty = account.get(field, 0) or 0
            status = "good"
            if year == CURRENT_YEAR and qty > 0:
                status = "upgrade"
            blade_expiry.append({"year": year, "quantity": qty, "status":status})
            # If you have separate Pogo expiry, replace above line with real field
            # For now, treat all as Blade if that's your convention

        return {
            "product_summary": product_summary,
            "batch_expiry": blade_expiry,
        }
    access_token, instance_url = get_salesforce_access_token(
        client_id=os.getenv('SALESFORCE_CLIENT_ID'),
        client_secret=os.getenv('SALESFORCE_CLIENT_SECRET'),
        username=os.getenv('SALESFORCE_USERNAME'),
        password=os.getenv('SALESFORCE_PASSWORD'),
        security_token=os.getenv('SALESFORCE_SECURITY_TOKEN')
    )
    site_code =  request.args.get('site_code')
    name = "Amazon " + site_code
    account_data = {}

    account = get_account_data(instance_url, access_token, name)
    dashboard_data = process_account_data(account['product'])
    account_data["part1"] = {"order":account["open_order"], "quotes":account["open_quote"]}
    account_data["part_2"] = dashboard_data
    insights = {"text":"No Batteries To Replace", "quantity":None, "type":"positive"}
    for x in dashboard_data['batch_expiry']:
        if int(x['year']) == CURRENT_YEAR and x['status'] == 'upgrade':
            insights = {"text":"Batteries Expiring Soon", "quantity":x['quantity'], "type":"alert"}
            break

    account_data["part_3"] = insights

    return jsonify(account_data), 200

@app.route('/update-address', methods=['POST'])
def update_address():
    data = request.get_json()
    # Get the account name from the request
    account_name = data.get('account_name') or data.get('site_code')  # Accept 'account_name' or 'site_code'
    if not account_name:
        return jsonify({'error': 'account_name or site_code is required'}), 400

    # Get the address fields
    shipping_data = {}
    if data.get('address_line_1'):
        shipping_data["ShippingStreet"] = data.get('address_line_1')
    if data.get('city'):
        shipping_data["ShippingCity"] = data.get('city')
    if data.get('state'):
        shipping_data["ShippingState"] = data.get('state')
    if data.get('zip'):
        shipping_data["ShippingPostalCode"] = data.get('zip')
    if data.get('country'):
        shipping_data["ShippingCountry"] = data.get('country')
    # Optionally, add Address Line 2
    if data.get('address_line_2'):
        shipping_data["ShippingStreet"] += f"\n{data.get('address_line_2')}"
    
    # Authenticate with Salesforce
    access_token, instance_url = get_salesforce_access_token(
        client_id=os.getenv('SALESFORCE_CLIENT_ID'),
        client_secret=os.getenv('SALESFORCE_CLIENT_SECRET'),
        username=os.getenv('SALESFORCE_USERNAME'),
        password=os.getenv('SALESFORCE_PASSWORD'),
        security_token=os.getenv('SALESFORCE_SECURITY_TOKEN')
    )

    # Find Account ID by Name
    soql = f"SELECT Id FROM Account WHERE Name = '{account_name}'"
    soql_url = f"{instance_url}/services/data/v60.0/query"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    resp = requests.get(soql_url, headers=headers, params={'q': soql})
    if resp.status_code != 200:
        return jsonify({'error': 'Failed to query Account', 'details': resp.text}), 500

    records = resp.json().get('records', [])
    if not records:
        return jsonify({'error': f"Account '{account_name}' not found"}), 404
    account_id = records[0]['Id']

    # Update Shipping Address
    update_url = f"{instance_url}/services/data/v60.0/sobjects/Account/{account_id}"
    update_resp = requests.patch(update_url, json=shipping_data, headers=headers)
    if update_resp.status_code == 204:
        return jsonify({'status': 'success', 'message': 'Shipping address updated'})
    else:
        return jsonify({'status': 'fail', 'error': update_resp.text}), update_resp.status_code

@app.route("/api/update-member", methods=["POST"])
def update_member():
    data = request.get_json()
    MS_SECRET = os.getenv("MEMBERSTACK_SECRET")
    BASE_URL = "https://admin.memberstack.com"
    HEADERS = {"X-API-KEY": MS_SECRET, "Content-Type": "application/json"}

    def get_member_id_by_email(email):
        url = f"{BASE_URL}/members/{urllib.parse.quote(email)}"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("id") or (data.get("data") or {}).get("id")

    try:
        email = data["email"]
        first_name = data.get("firstName")
        last_name = data.get("lastName")
        job_title = data.get("jobTitle")
        amazon_site = data.get("amazonSite")

        # Step 1: lookup member ID
        member_id = get_member_id_by_email(email)
        if not member_id:
            return jsonify({"error": "Member not found"}), 404

        # Step 2: update member fields
        payload = {
            "customFields": {
                "first-name": first_name,
                "last-name": last_name,
                "job-title": job_title,
                "amazon-site": amazon_site
            }
        }
        resp = requests.patch(
            f"{BASE_URL}/members/{member_id}",
            headers=HEADERS,
            json=payload,
            timeout=10
        )
        resp.raise_for_status()

        return jsonify({"status": "success", "memberId": member_id, "updated": resp.json()})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
ALL_SITES =  ["Amazon ABE2",
 "Amazon ABE3",
 "Amazon ABE8",
 "Amazon ABQ1",
 "Amazon ABQ2",
 "Amazon ABQ5",
 "Amazon ACY1",
 "Amazon ACY2",
 "Amazon ACY5",
 "Amazon ACY8",
 "Amazon ACY9",
 "Amazon AFW1",
 "Amazon AFW2",
 "Amazon AFW5",
 "Amazon AGS1",
 "Amazon AGS2",
 "Amazon AGS5",
 "Amazon AKC1",
 "Amazon AKH4",
 "Amazon AKR1",
 "Amazon ALB1",
 "Amazon AMA1",
 "Amazon AMZ9",
 "Amazon ATL2",
 "Amazon ATL5",
 "Amazon ATL6",
 "Amazon ATL7",
 "Amazon ATS3",
 "Amazon ATS5",
 "Amazon AUN2",
 "Amazon AUS3",
 "Amazon AUS5",
 "Amazon AUV1",
 "Amazon AVP1",
 "Amazon AVP8",
 "Amazon AVP9",
 "Amazon AZA4",
 "Amazon AZA5",
 "Amazon BAN2",
 "Amazon BDL2",
 "Amazon BDL3",
 "Amazon BDL4",
 "Amazon BDL6",
 "Amazon BDL7",
 "Amazon BDU2",
 "Amazon BFi3",
 "Amazon BFI4",
 "Amazon BFI5",
 "Amazon BFI7",
 "Amazon BFI9",
 "Amazon BFL1",
 "Amazon BFL2",
 "Amazon BHM1",
 "Amazon BJX1",
 "Amazon BLV2",
 "Amazon BNA2",
 "Amazon BNA3",
 "Amazon BNA5",
 "Amazon BNA6",
 "Amazon BNA7",
 "Amazon BNA8",
 "Amazon BNE1",
 "Amazon BOI2",
 "Amazon BOI5",
 "Amazon BOS12",
 "Amazon BOS27",
 "Amazon BOS3",
 "Amazon BOS4",
 "Amazon BTR1",
 "Amazon BUF5",
 "Amazon BUF9",
 "Amazon BUR7",
 "Amazon BWI1",
 "Amazon BWI2",
 "Amazon BWI4",
 "Amazon BWI5",
 "Amazon BWU1",
 "Amazon BWU6",
 "Amazon CAE1",
 "Amazon CAE3",
 "Amazon CAK4",
 "Amazon CDW5",
 "Amazon CHA1",
 "Amazon CHA2",
 "Amazon CHM5",
 "Amazon CLE2",
 "Amazon CLE3",
 "Amazon CLE5",
 "Amazon CLE7",
 "Amazon CLE9",
 "Amazon CLT2",
 "Amazon CLT3",
 "Amazon CLT4",
 "Amazon CLT5",
 "Amazon CLT6",
 "Amazon CLT9",
 "Amazon CMH1",
 "Amazon CMH3",
 "Amazon CMH4",
 "Amazon CMH5",
 "Amazon CNO5",
 "Amazon CNO8",
 "Amazon COS5",
 "Amazon CRG1",
 "Amazon CSG1",
 "Amazon CVG2",
 "Amazon CVG5",
 "Amazon CVG9",
 "Amazon DAB2",
 "Amazon DAE1",
 "Amazon DAE3",
 "Amazon DAL2",
 "Amazon DAL3",
 "Amazon DAL9",
 "Amazon DAU1",
 "Amazon DAU2",
 "Amazon DAZ4",
 "Amazon DBA5",
 "Amazon DBA8",
 "Amazon DBK1",
 "Amazon DBL1",
 
 "Amazon DBO6",
 "Amazon DBO7",
 "Amazon DBU1",
 "Amazon DBV1",
 "Amazon DCA1",
 "Amazon DCA2",
 "Amazon DCA6",
 "Amazon DCD6",
 "Amazon DCG2",
 "Amazon DCG4",
 "Amazon DCH6",
 "Amazon DCK1",
 "Amazon DCL2",
 "Amazon DCL3",
 "Amazon DCL4",
 "Amazon DCM2",
 "Amazon DCS3",
 "Amazon DCW1",
 "Amazon DCW8",
 "Amazon DDC3",
 "Amazon DDC4",
 "Amazon DDE6",
 "Amazon DDE8",
 "Amazon DDF2",
 "Amazon DDT1",
 "Amazon DEN2",
 "Amazon DEN3",
 "Amazon DEN5",
 "Amazon DEN7",
 "Amazon DEN8",
 "Amazon DET1",
 "Amazon DET2",
 "Amazon DET3",
 "Amazon DET6",
 "Amazon DET7",
 "Amazon DEW5",
 "Amazon DFH3",
 "Amazon DFL4",
 "Amazon DFM3",
 "Amazon DFT4",
 "Amazon DFW5",
 "Amazon DFW6",
 "Amazon DFW7",
 "Amazon DFX3",
 "Amazon DGE7",
 "Amazon DGI3",
 
 "Amazon DHT4",
 "Amazon DIB7",
 "Amazon DID2",
 "Amazon DIL3",
 "Amazon DIL5",
 "Amazon DIN3",
 "Amazon DJE1",
 "Amazon DJE2",
 "Amazon DJE3",
 "Amazon DJE9",
 "Amazon DJX2",
 "Amazon DJX4",
 "Amazon DJZ3",
 "Amazon DJZ6",
 "Amazon DKC3",
 "Amazon DKO9",
 "Amazon DKS3",
 "Amazon DKY9",
 "Amazon DLC8",
 "Amazon DLD1",
 "Amazon DLI4",
 "Amazon DLI6",
 "Amazon DLN2",
 "Amazon DLN3",
 "Amazon DLT3",
 "Amazon DLT6",
 "Amazon DLV3",
 "Amazon DMD2",
 "Amazon DMD9",
 "Amazon DMI7",
 "Amazon DML1",
 "Amazon DML6",
 "Amazon DMO3",
 "Amazon DMO4",
 "Amazon DMP1",
 "Amazon DMT1",
 "Amazon DMW1",
 "Amazon DNA6",
 "Amazon DNH2",
 "Amazon DNK7",
 "Amazon DOI4",
 "Amazon DOI6",
 "Amazon DOK2",
 "Amazon DON3",
 "Amazon DON9",
 "Amazon DPD2",
 "Amazon DPD4",
 "Amazon DPH7",
 "Amazon DPL2.",
 "Amazon DPP1",
 "Amazon DRC6",
 "Amazon DRT3",
 "Amazon DRT8",
 "Amazon DSC3",
 "Amazon DSC4",
 "Amazon DSD4",
 "Amazon DSF5",
 "Amazon DSM5",
 "Amazon DSM9",
 "Amazon DSW3",
 "Amazon DSX7",
 "Amazon DTB4",
 "Amazon DTN6",
 "Amazon DTO3",
 "Amazon DTO5",
 "Amazon DTO9",
 "Amazon DTU2",
 "Amazon DTU8",
 "Amazon DTW1",
 "Amazon DTW3",
 "Amazon DTW8",
 "Amazon DTW9",
 "Amazon DUT2",
 "Amazon DUT4",
 "Amazon DVB8",
 "Amazon DVY2",
 "Amazon DWA6",
 "Amazon DWD6",
 "Amazon DXT5",
 "Amazon DYN3",
 "Amazon DYT3",
 "Amazon DYY6",
 "Amazon ELP1",
 "Amazon EUG5",
 "Amazon EWR4",
 "Amazon EWR7",
 "Amazon EWR8",
 "Amazon FAR1",
 "Amazon FAT1",
 "Amazon FAT2",
 "Amazon FAT5",
 "Amazon FOE1",
 "Amazon FR1",
 "Amazon FSD1",
 "Amazon FTW1",
 "Amazon FTW2",
 "Amazon FTW3",
 "Amazon FTW5",
 "Amazon FTW6",
 "Amazon FTW7",
 "Amazon FTW8",
 "Amazon FTW9",
 "Amazon FTY9",
 "Amazon FWA4",
 "Amazon FWA6",
 "Amazon GDL1",
 "Amazon GDL2",
 "Amazon GEG1",
 "Amazon GEG2",
 "Amazon GEU2",
 "Amazon GEU3",
 "Amazon GEU5",
 "Amazon GLD2",
 "Amazon GRR1",
 "Amazon GRU8",
 "Amazon GSO1",
 "Amazon GSP1",
 "Amazon GYR1",
 "Amazon GYR2",
 "Amazon GYR3",
 "Amazon GYR4",
 "Amazon HAT2",
 "Amazon HAT9",
 "Amazon HBA3",
 "Amazon HBA9",
 "Amazon HBF5",
 "Amazon HBI2",
 "Amazon HBN9",
 "Amazon HCE2",
 "Amazon HCH2",
 "Amazon HCL9",
 "Amazon HCM9",
 "Amazon HCN1",
 "Amazon HDA3",
 "Amazon HDA9",
 "Amazon HDC3",
 "Amazon HDS9",
 "Amazon HDT9",
 "Amazon HEA1",
 "Amazon HEA2",
 "Amazon HEW9",
 "Amazon HFD5",
 "Amazon HGA3",
 "Amazon HGA6",
 "Amazon HGE2",
 "Amazon HGR2",
 "Amazon HGR5",
 "Amazon HGR6",
 "Amazon HHO2",
 "Amazon HHO3",
 "Amazon HIA1",
 "Amazon HIL3",
 "Amazon HIO2",
 "Amazon HLA6",
 "Amazon HLA8",
 "Amazon HLA9",
 "Amazon HLI1",
 "Amazon HLI2",
 "Amazon HLO9",
 "Amazon HLR1",
 "Amazon HLX1",
 "Amazon HMC9",
 "Amazon HMD3",
 "Amazon HME9",
 "Amazon HMI2",
 "Amazon HMK9",
 "Amazon HMO2",
 "Amazon HMO3",
 "Amazon HMS9",
 "Amazon HMW1",
 "Amazon HMW3",
 "Amazon HMW4",
 "Amazon HMY1",
 "Amazon HNC3",
 "Amazon HNE1",
 "Amazon HNY2",
 "Amazon HOU1",
 "Amazon HOU2",
 "Amazon Hou3",
 "Amazon HOU5",
 "Amazon HOU6",
 "Amazon HOU7",
 "Amazon HOU8",
 "Amazon HOU9",
 "Amazon HRN2",
 "Amazon HSD1",
 "Amazon HSE1",
 "Amazon HSF2",
 "Amazon HSL9",
 "Amazon HSV1",
 "Amazon HSV2",
 "Amazon HTC2",
 "Amazon HTP2",
 "Amazon HWA4",
 "Amazon HWE2",
 "Amazon HYC2",
 "Amazon HYE1",
 "Amazon HYO1",
 "Amazon HYV1",
 "Amazon IAH1",
 "Amazon IAH3",
 "Amazon IAH5",
 "Amazon ICT2",
 "Amazon IDT1",
 "Amazon IGQ1",
 "Amazon IGQ2",
 "Amazon IND1",
 "Amazon IND2",
 "Amazon IND5",
 "Amazon IND8",
 "Amazon IND9",
 "Amazon JAN1",
 "Amazon JAN1",
 "Amazon JAX2",
 "Amazon JAX3",
 "Amazon JAX5",
 "Amazon JAX7",
 "Amazon JAX9",
 "Amazon JFK2",
 "Amazon JFK8",
 "Amazon JHW1",
 "Amazon JVL1",
 "Amazon KAFW",
 "Amazon KBWI",
 "Amazon KCVG",
 "Amazon KIL1",
 "Amazon KILN",
 "Amazon KLAL",
 "Amazon KOH2",
 "Amazon KRB1",
 "Amazon KRB2",
 "Amazon KRB3",
 "Amazon KRB4",
 "Amazon KRB5",
 "Amazon KRB7",
 "Amazon KRB9",
 "Amazon KRFD",
 "Amazon KSBD",
 "Amazon Kuiper",
 "Amazon LAL4",
 "Amazon LAN2",
 "Amazon LAS1",
 "Amazon LAS2",
 "Amazon LAS6",
 "Amazon LAS7",
 "Amazon LAS8",
 "Amazon LAX5",
 "Amazon LAX9",
 "Amazon LBB5",
 "Amazon LBE1",
 "Amazon LDJ5",
 "Amazon LEX1",
 "Amazon LFT1",
 "Amazon LGA5",
 "Amazon LGA9",
 "Amazon LGB3",
 "Amazon LGB4",
 "Amazon LGB5",
 "Amazon LGB6",
 "Amazon LGB7",
 "Amazon LGB8",
 "Amazon LGB9",
 "Amazon LIT1",
 "Amazon LIT2",
 "Amazon LUK2",
 "Amazon LUK7",
 "Pillpack MAN1",
 "Amazon MCE1",
 "Amazon MCI3",
 "Amazon MCI7",
 "Amazon MCI9",
 "Amazon MCO1",
 "Amazon MCO2",
 "Amazon MCO3",
 "Amazon MCO4",
 "Amazon MCO5",
 "Amazon MCO9",
 "Amazon MDT4",
 "Amazon MDT5",
 "Amazon MDT9",
 "Amazon MDW2",
 "Amazon MDW4",
 "Amazon MDW5",
 "Amazon MDW7",
 "Amazon MDW8",
 "Amazon MDW9",
 "Amazon MEL1",
 "Amazon MEL5",
 "Amazon MEL8",
 "Amazon MEM1",
 "Amazon MEM2",
 "Amazon MEM3",
 "Amazon MEM4",
 "Amazon MEM5",
 "Amazon MEM6",
 "Amazon MEM8",
 "Amazon MEX1",
 "Amazon MEX2",
 "Amazon MEX5",
 "Amazon MEX6",
 "Amazon MGE1",
 "Amazon MGE3",
 "Amazon MGE5",
 "Amazon MGE8",
 "Amazon MGE9",
 "Amazon MIA1",
 "Amazon MIA2",
 "Amazon MIA5",
 "Amazon MIT2",
 "Amazon MKC4",
 "Amazon MKC6",
 "Amazon MKE1",
 "Amazon MKE2",
 "Amazon MKE5",
 "Amazon MLB1",
 "Amazon MLI1",
 "Amazon MMU9",
 "Amazon MOB5",
 "Amazon MQJ1",
 "Amazon MQJ2",
 "Amazon MQJ5",
 "Amazon MQY1",
 "Amazon MSP1",
 "Amazon MSP6",
 "Amazon MSP7",
 "Amazon MSP8",
 "Amazon MSP9",
 "Amazon MTN1",
 "Amazon MTN2",
 "Amazon MTN3",
 "Amazon MTN6",
 "Amazon MTN7",
 "Amazon MTN8",
 "Amazon MTN9",
 "Amazon MTY1",
 "Amazon MTY2",
 "Amazon MTY5",
 "Amazon MWH1",
 "Amazon OAK3",
 "Amazon OAK4",
 "Amazon OAK5",
 "Amazon OAK7",
 "Amazon OAK9",
 "Amazon OKC1",
 "Amazon OKC2",
 "Amazon OKC5",
 "Amazon OLM1",
 "Amazon OMA2",
 "Amazon OMA5",
 "Amazon ONT1",
 "Amazon ONT2",
 "Amazon ONT5",
 "Amazon ONT6",
 "Amazon ONT8",
 "Amazon ONT9",
 "Amazon ORD2",
 "Amazon ORD4",
 "Amazon ORD5",
 "Amazon ORD9",
 "Amazon ORF2",
 "Amazon ORF3",
 "Amazon ORH3",
 "Amazon ORH5",
 "Amazon Otter",
 "Amazon OWD5",
 "Amazon OWD9",
 "Amazon OXR1",
 "Amazon PAE2",
 "Amazon Parent",
 "Pillpack PAU2",
 "Amazon PBI2",
 "Amazon PBI3",
 "Amazon PCA1",
 "Amazon PCW1",
 "Amazon PDK2",
 "Amazon PDX5",
 "Amazon PDX6",
 "Amazon PDX7",
 "Amazon PDX8",
 "Amazon PDX9",
 "Amazon PER3",
 "Amazon PGA1",
 "Amazon PHL1",
 "Amazon PHL4",
 "Amazon PHL5",
 "Amazon PHL6",
 "Amazon PHL7",
 "Amazon PHL9",
 "Amazon PHX3",
 "Amazon PHX5",
 "Amazon PHX6",
 "Amazon PHX7",
 "Amazon PIN2",
 "Amazon PIT2",
 "Amazon PIT4",
 "Amazon PIT5",
 "Amazon PIT9",
 "Amazon PKC1",
 "Amazon PillPack PMI2",
 "Amazon PNA1",
 "Amazon PNE5",
 "Amazon POC1",
 "Amazon POC2",
 "Amazon POC3",
 "Amazon POH2",
 "Amazon POR2",
 "Amazon PPA2",
 "Amazon PPO4",
 "Amazon PPX1",
 "Amazon PSC2",
 "PillPack PSE1",
 "Amazon PSP1",
 "Amazon PVD2",
 "Amazon QXY8",
 "Amazon RAD1",
 "Amazon RBD5",
 "Amazon RDG1",
 "Amazon RDU1",
 "Amazon RDU2",
 "Amazon RDU4",
 "Amazon RDU5",
 "Amazon RDU9",
 "Amazon RFD1",
 "Amazon RFD2",
 "Amazon RFD3",
 "Amazon RFD4",
 "Amazon RFD7",
 "Amazon RIC1",
 "Amazon RIC2",
 "Amazon RIC3",
 "Amazon RIC5",
 "Amazon RMN3",
 "Amazon RNO4",
 "Amazon RNT9",
 "Amazon Robotics",
 "Amazon ROC1",
 "Amazon RSW5",
 "Amazon RYY2",
 "Amazon SAN3",
 "Amazon SAN5",
 "Amazon SAT1",
 "Amazon SAT2",
 "Amazon SAT3",
 "Amazon SAT4",
 "Amazon SAT9",
 "Amazon SAV3",
 "Amazon SAV4",
 "Amazon SAV7",
 "Amazon SAX1",
 "Amazon SAX2",
 "Amazon SAX3",
 "Amazon SAX5-2",
 "Amazon SAX7-1",
 "Amazon SAZ1",
 "Amazon SAZ2",
 "Amazon SBD2",
 "Amazon SBD3",
 "Amazon SBD5",
 "Amazon SBD6",
 "Amazon SCA2",
 "Amazon SCA3",
 "Amazon SCA5",
 "Amazon SCA7",
 "Amazon SCK1",
 "Amazon SCK3",
 "Amazon SCK4",
 "Amazon SCK6",
 "Amazon SCK8",
 "Amazon SCO1",
 "Amazon SDC1",
 "Amazon SDF1",
 "Amazon SDF6",
 "Amazon SDF8",
 "Amazon SDF9",
 "Amazon SEA124",
 "Amazon SFL1",
 "Amazon SFL3",
 "Amazon SFL4",
 "Amazon SFL6",
 "Amazon SFL7",
 "Amazon SFL8",
 "Amazon SGA1",
 "Amazon SGA2",
 "Amazon SHV1",
 "Amazon SIL1",
 "Amazon SIL3",
 "Amazon SIL4",
 "Amazon SIN8",
 "Amazon SIN9",
 "Amazon SJC7",
 "Amazon SLC1",
 "Amazon SLC2",
 "Amazon SLC3",
 "Amazon SLC4",
 "Amazon SLC9",
 "Amazon SMA1",
 "Amazon SMA2",
 "Amazon SMD1",
 "Amazon SMD2",
 "Amazon SMF1",
 "Amazon SMF3",
 "Amazon SMF5",
 "Amazon SMF6",
 "Amazon SMF7",
 "Amazon SMI1",
 "Amazon SMN1",
 "Amazon SMO1",
 "Amazon SMO2",
 "Amazon SNA3",
 "Amazon SNA4",
 "Amazon SNC3",
 "Amazon SNE1",
 "Amazon SNJ1",
 "Amazon SNJ2",
 "Amazon SNJ3",
 "Amazon SNV1",
 "Amazon SNY1",
 "Amazon SNY2",
 "Amazon SNY5",
 "Amazon SOH1",
 "Amazon SOH2",
 "Amazon SOH3",
 "Amazon SOR3",
 "Amazon STL3",
 "Amazon STL4",
 "Amazon STL5",
 "Amazon STL6",
 "Amazon STL8",
 "Amazon STL9",
 "Amazon STN1",
 "Amazon STP2",
 "Amazon STX2",
 "Amazon STX5",
 "Amazon STX7",
 "Amazon STX8",
 "Amazon STX9",
 "Amazon SUT1",
 "Amazon SUT2",
 "Amazon SWA1",
 "Amazon SWA2",
 "Amazon SWF1",
 "Amazon SWF2",
 "Amazon SWI1",
 "Amazon SXWL",
 "Amazon SYR1",
 "Amazon SYS3",
 "Amazon TCY1",
 "Amazon TCY2",
 "Amazon TCY5",
 "Amazon TCY9",
 "Amazon TEB3",
 "Amazon TEB4",
 "Amazon TEB6",
 "Amazon TEB9",
 "Amazon TEN1",
 "Amazon TIJ1",
 "Amazon TLH2",
 "Amazon TMB8",
 "Amazon TPA1",
 "Amazon TPA2",
 "Amazon TPA3",
 "Amazon TPA4",
 "Amazon TPA6",
 "Amazon TTN2",
 "Amazon TUL2",
 "Amazon TUL5",
 "Amazon TUS1",
 "Amazon TUS2",
 "Amazon TUS5",
 "Amazon TYS1",
 "Amazon TYS5",
 "Amazon UAZ1",
 "Amazon UCA5",
 "Amazon UCO1",
 "Amazon UFL4",
 "Amazon UFL5",
 "Amazon UGA2",
 "Amazon UGA4",
 "Amazon UIN1",
 "Amazon UMD1",
 "Amazon UMN1",
 "Amazon UNC2",
 "Amazon UNC3",
 "Amazon UNJ1",
 "Amazon UNV2",
 "Amazon UNY2",
 "Amazon UOH4",
 "Amazon UOH5",
 "Amazon UOR2",
 "Amazon UPA1",
 "Amazon USD1",
 "Amazon USF1",
 "Amazon USF2",
 "Amazon UTN1",
 "Amazon UTX3",
 "Amazon UTX4",
 "Amazon UTX8",
 "Amazon UTX9",
 "Amazon UVA1",
 "Amazon UVA5",
 "Amazon UWA1",
 "Amazon UWA2",
 "Amazon UWA6",
 "Amazon VGT1",
 "Amazon VGT2",
 "Amazon VGT5",
 "Amazon VUGP",
 "Amazon WBW2",
 "Amazon WFL2",
 "Amazon WGE2",
 "Amazon WID1",
 "Amazon WIL1",
 "Amazon WLF2",
 "Amazon WMS1",
 "Amazon WNE1",
 "Amazon WNY4",
 "Amazon WTX2",
 "Amazon WWY3",
 "Amazon WWY4",
 "Amazon XCA2",
 "Amazon XCH1",
 "Amazon XCL1",
 "Amazon XEW3",
 "Amazon XEW4",
 "Amazon XHH3",
 "Amazon XLX3",
 "Amazon XLX7",
 "Amazon XME1",
 "Amazon XNJ2",
 "Amazon XPH1",
 "Amazon XSE2",
 "Amazon XUSO",
 "Amazon YEG1",
 "Amazon YEG4",
 "Amazon YGK1",
 "Amazon YHM1",
 "Amazon YHM2",
 "Amazon YHM5",
 "Amazon YHM6",
 "Amazon YHM8",
 "Amazon YHM9",
 "Amazon YOW1",
 "Amazon YOW3",
 "Amazon YUL9",
 "Amazon YVR2",
 "Amazon YVR3",
 "Amazon YVR4",
 "Amazon YVR7",
 "Amazon YXU1",
 "Amazon YXX1",
 "Amazon YXX2",
 "Amazon YYC1",
 "Amazon YYC4",
 "Amazon YYC5",
 "Amazon YYC6",
 "Amazon YYZ1",
 "Amazon YYZ3",
 "Amazon YYZ4",
 "Amazon YYZ7",
 "Amazon YYZ9",
]
@app.route("/sites")
def get_sites():
    q = request.args.get("q", "").lower()
    results = [s for s in ALL_SITES if q in s.lower()] if q else ALL_SITES
    return jsonify(results[:50])

if __name__ == '__main__':
    app.run(debug=True, port=os.getenv('PORT', 5000))
