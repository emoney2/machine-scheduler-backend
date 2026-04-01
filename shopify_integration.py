"""
Shopify Integration for Product Builder

This module provides backend endpoints for:
1. Storing product builder orders
2. Receiving Shopify webhooks
3. Syncing Shopify orders to the web app

To use: Import and call register_shopify_routes(app) in your Flask app
"""

from flask import request, jsonify
import logging
import json
import base64
import hmac
import hashlib
from datetime import datetime
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
import io

# Configuration
SHOPIFY_WEBHOOK_SECRET = os.getenv('SHOPIFY_WEBHOOK_SECRET', '')

# Email configuration
SMTP_SERVER = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_EMAIL = os.getenv('SMTP_EMAIL', '')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', '')
RECIPIENT_EMAIL = os.getenv('RECIPIENT_EMAIL', SMTP_EMAIL)  # Where to send customer questions

# Supabase (if available for saving designs)
try:
    from supabase import create_client
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if (SUPABASE_URL and SUPABASE_KEY) else None
except:
    supabase = None

def register_shopify_routes(app):
    """
    Register Shopify routes with the Flask app.
    Call this function after creating your Flask app instance.
    """
    @app.route('/api/product-builder/order', methods=['POST'])
    def save_product_builder_order():
        """
        Save product builder order configuration to backend.
        This stores the order before it goes to Shopify cart.
        """
        try:
            order_data = request.get_json()
            
            # Validate required fields
            required_fields = ['productType', 'materialColor', 'furColor', 'price']
            for field in required_fields:
                if field not in order_data:
                    return jsonify({'error': f'Missing required field: {field}'}), 400
            
            # Generate order ID
            order_id = f"PB-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{int(datetime.now().timestamp())}"
            order_data['orderId'] = order_id
            order_data['status'] = 'pending'
            order_data['savedAt'] = datetime.now().isoformat()
            
            # TODO: Save to database or Google Sheets
            # For now, log it
            logging.info(f'Product Builder Order: {order_id}', extra={'order_data': order_data})
            
            # TODO: Generate Shopify checkout URL or return cart info
            return jsonify({
                'success': True,
                'orderId': order_id,
                'message': 'Order configuration saved'
            }), 200
            
        except Exception as e:
            logging.exception('Error saving product builder order')
            return jsonify({'error': str(e)}), 500

    # POST /api/shopify/webhook/orders/create is defined in server.py (shopify_webhook_orders_create):
    # Production Orders sheet, Google Drive folders, preview_{order}.jpg, order_*_logo_*.png.
    # A duplicate route here caused Flask to match this stub first and skip the real handler.

    def verify_webhook(data, hmac_header):
        """
        Verify Shopify webhook HMAC signature.
        """
        if not SHOPIFY_WEBHOOK_SECRET:
            logging.warning('SHOPIFY_WEBHOOK_SECRET not set, skipping webhook verification')
            return True
        
        calculated_hmac = base64.b64encode(
            hmac.new(
                SHOPIFY_WEBHOOK_SECRET.encode('utf-8'),
                data,
                hashlib.sha256
            ).digest()
        ).decode()
        
        return hmac.compare_digest(calculated_hmac, hmac_header)


    @app.route('/api/shopify/sync-orders', methods=['POST'])
    def sync_shopify_orders():
        """
        Manually sync orders from Shopify Admin API.
        This can be called periodically to pull new orders.
        """
        # TODO: Implement Shopify Admin API integration
        # This would use the Shopify Python API or REST API to fetch orders
        # and process product builder orders
        
        return jsonify({
            'message': 'Order sync endpoint - to be implemented',
            'instructions': 'Use Shopify Admin API to fetch orders and process product builder orders'
        }), 200


    @app.route('/api/product-builder/save-design', methods=['POST'])
    def save_design():
        """
        Save user design to their account.
        Supports two actions:
        - 'signin': Sign in with username/email and password, then save design
        - 'create': Create new account with first name, last name, email/username, password, phone, then save design
        """
        try:
            data = request.get_json()
            action = data.get('action', 'create')  # Default to create for backwards compatibility
            
            if not data.get('design'):
                return jsonify({'error': 'Missing design data'}), 400
            
            design_data = data['design']
            
            # Hash password (simple hash for now - in production use bcrypt)
            import hashlib
            
            # Generate design ID
            design_id = f"DESIGN-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{int(datetime.now().timestamp())}"
            design_data['designId'] = design_id
            
            user_id = None
            email = None
            username = None
            
            # Save to Supabase if available
            if supabase:
                try:
                    if action == 'signin':
                        # Sign in flow
                        username = data.get('username', '').strip()
                        password = data.get('password', '')
                        
                        if not username or not password:
                            return jsonify({'error': 'Missing required fields: username, password'}), 400
                        
                        password_hash = hashlib.sha256(password.encode()).hexdigest()
                        
                        # Try to find user by email or username
                        user_query = supabase.table('product_builder_users').select('id, email, username, password_hash').or_(f'email.eq.{username},username.eq.{username}').execute()
                        
                        if not user_query.data or len(user_query.data) == 0:
                            return jsonify({'error': 'Invalid username or password'}), 401
                        
                        user = user_query.data[0]
                        
                        # Verify password
                        if user['password_hash'] != password_hash:
                            return jsonify({'error': 'Invalid username or password'}), 401
                        
                        user_id = user['id']
                        email = user.get('email', username)
                        
                    elif action == 'create':
                        # Create account flow
                        first_name = data.get('firstName', '').strip()
                        last_name = data.get('lastName', '').strip()
                        email = data.get('email', '').strip()
                        username = data.get('username', email)  # Use email as username if not provided
                        password = data.get('password', '')
                        phone = data.get('phone', '').strip()
                        
                        if not first_name or not last_name or not email or not password:
                            return jsonify({'error': 'Missing required fields: firstName, lastName, email, password'}), 400
                        
                        if len(password) < 6:
                            return jsonify({'error': 'Password must be at least 6 characters'}), 400
                        
                        password_hash = hashlib.sha256(password.encode()).hexdigest()
                        
                        # Check if user already exists
                        existing_user = supabase.table('product_builder_users').select('id').or_(f'email.eq.{email},username.eq.{username}').execute()
                        
                        if existing_user.data and len(existing_user.data) > 0:
                            return jsonify({'error': 'An account with this email or username already exists'}), 409
                        
                        # Create new user
                        user_result = supabase.table('product_builder_users').insert({
                            'email': email,
                            'username': username,
                            'first_name': first_name,
                            'last_name': last_name,
                            'phone': phone if phone else None,
                            'password_hash': password_hash,
                            'created_at': datetime.now().isoformat()
                        }).execute()
                        
                        if user_result.data and len(user_result.data) > 0:
                            user_id = user_result.data[0]['id']
                        else:
                            return jsonify({'error': 'Failed to create account'}), 500
                    
                    if user_id:
                        # Save design
                        design_result = supabase.table('product_builder_designs').insert({
                            'user_id': user_id,
                            'design_id': design_id,
                            'design_data': json.dumps(design_data),
                            'created_at': datetime.now().isoformat()
                        }).execute()
                        
                        logging.info(f'Design saved to Supabase: {design_id} for user {email or username}')
                        
                except Exception as e:
                    logging.warning(f'Supabase save failed, using fallback: {e}')
                    # Continue with fallback logging
            else:
                # If no Supabase, validate inputs for logging
                if action == 'signin':
                    username = data.get('username', '').strip()
                    password = data.get('password', '')
                    if not username or not password:
                        return jsonify({'error': 'Missing required fields: username, password'}), 400
                    email = username
                elif action == 'create':
                    email = data.get('email', '').strip()
                    username = data.get('username', email)
                    if not email:
                        return jsonify({'error': 'Missing required fields'}), 400
            
            # Fallback: Log to file/system (for development)
            name = f"{data.get('firstName', '')} {data.get('lastName', '')}".strip() or username or email
            logging.info(f'Product Builder Design Saved: {design_id}', extra={
                'action': action,
                'user_email': email,
                'user_name': name,
                'design_id': design_id
            })
            
            return jsonify({
                'success': True,
                'designId': design_id,
                'message': 'Design saved successfully'
            }), 200
            
        except Exception as e:
            logging.exception('Error saving design')
            return jsonify({'error': str(e)}), 500


    @app.route('/api/product-builder/send-question', methods=['POST'])
    def send_question():
        """
        Send email with customer question and screenshot of their design.
        """
        try:
            data = request.get_json()
            
            # Validate required fields
            if not data.get('name') or not data.get('email') or not data.get('message'):
                return jsonify({'error': 'Missing required fields: name, email, message'}), 400
            
            name = data['name']
            email = data['email']
            phone = data.get('phone', '')
            message = data['message']
            screenshot = data.get('screenshot', '')  # Base64 encoded image
            design_state = data.get('designState', {})
            
            # Prepare email
            if not SMTP_EMAIL or not SMTP_PASSWORD:
                logging.warning('Email configuration not set, logging question instead')
                logging.info(f'Customer Question from {name} ({email}): {message}')
                return jsonify({
                    'success': True,
                    'message': 'Question logged (email not configured)'
                }), 200
            
            # Create email
            msg = MIMEMultipart()
            msg['From'] = SMTP_EMAIL
            msg['To'] = RECIPIENT_EMAIL
            msg['Subject'] = f'Product Builder Question from {name}'
            msg['Reply-To'] = email
            
            # Email body
            body = f"""
Customer Question - Product Builder

Name: {name}
Email: {email}
Phone: {phone if phone else 'Not provided'}

Message:
{message}

Design Details:
- Product Type: {design_state.get('productType', 'N/A')}
- Material: {design_state.get('materialColor', 'N/A')}
- Fur Color: {design_state.get('furColor', 'N/A')}
- Has Logo: {'Yes' if design_state.get('hasLogo') else 'No'}
- Text Elements: {design_state.get('textCount', 0)}

---
This message was sent from the Product Builder.
Please reply directly to {email} to respond to the customer.
"""
            
            msg.attach(MIMEText(body, 'plain'))
            
            # Attach screenshot if provided
            if screenshot:
                try:
                    # Remove data URL prefix if present
                    if ',' in screenshot:
                        header, data = screenshot.split(',', 1)
                        image_data = base64.b64decode(data)
                    else:
                        image_data = base64.b64decode(screenshot)
                    
                    img = MIMEImage(image_data)
                    img.add_header('Content-Disposition', 'attachment', filename='design_screenshot.png')
                    msg.attach(img)
                except Exception as e:
                    logging.warning(f'Failed to attach screenshot: {e}')
            
            # Send email
            try:
                with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                    server.starttls()
                    server.login(SMTP_EMAIL, SMTP_PASSWORD)
                    server.send_message(msg)
                
                logging.info(f'Question email sent from {name} ({email})')
                return jsonify({
                    'success': True,
                    'message': 'Question sent successfully'
                }), 200
            except Exception as e:
                logging.error(f'Failed to send email: {e}')
                return jsonify({'error': f'Failed to send email: {str(e)}'}), 500
            
        except Exception as e:
            logging.exception('Error sending question email')
            return jsonify({'error': str(e)}), 500
