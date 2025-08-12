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
        url = f"{instance_url}/services/data/v60.0/sobjects/gii__SalesQuote__c/"
        payload = {
            "gii__Account__c": account_id,
            "Quote_Name__c": f"Test Quote on {datetime.strftime(datetime.now(),'%d %B %Y %H:%M')}",
            "gii__Status__c": "Open",
            "gii__SalesRepresentative__c": "0031I000009dExWQAU",
            "OwnerId": "0051I000001qk6a",
        }
        r = requests.post(url, headers=headers, json=payload)
        if r.status_code == 201:
            print("✅ Created Sales Quote")
            return r.json()['id']
        else:
            print("❌ Failed to create Sales Quote:", r.text)
            return None

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
    
    sales_quote_id = create_sales_quote(account_id)
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

    # At the end of quote() BEFORE return:
    send_email_async({
        "link": link,  # Salesforce quote link
        "created_by_email": user.get('auth', {}).get('email', ''),
        "first_name": user.get('customFields', {}).get('first-name', ''),
        "last_name": user.get('customFields', {}).get('last-name', ''),
        "account_name": account_name,
        "address_changed": change,
        "shipping_address": shipping_address,
        "products": products
    })

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
        <h2>New Sales Quote Created</h2>
        <p><b>Account:</b> {data.get('account_name','')}</p>
        <p><b>Created by:</b> {data.get('first_name','')} {data.get('last_name','')} ({data.get('created_by_email','')})</p>
        <p><b>Address Changed?</b> {"Yes" if data.get('address_changed') == "Y" else "No"}</p>
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

if __name__ == '__main__':
    app.run(debug=True, port=os.getenv('PORT', 5000))
