from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

client = GoogleAdsClient.load_from_storage("google-ads.yaml")
service = client.get_service("CustomerService")

try:
    accessible = service.list_accessible_customers()
    print("Connection successful!")
    print(f"Accessible customer IDs: {accessible.resource_names}")
except GoogleAdsException as ex:
    for error in ex.failure.errors:
        print(f"Error: {error.message}")
    raise
