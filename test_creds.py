import json
from google.oauth2 import service_account

# Load the JSON from the file you mounted
with open('credentials.json', 'r') as f:
    data = json.load(f)

# Print out the key ID so we know we actually loaded it
print("private_key_id:", data.get("private_key_id"))

# Try instantiating the Credentials object
creds = service_account.Credentials.from_service_account_info(data)

# If that succeeded, print the service account email
print("service_account_email:", creds.service_account_email)
