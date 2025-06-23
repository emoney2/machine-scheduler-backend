import os
import requests
import xmltodict

# Load your UPS credentials from environment
UPS_ACCESS_KEY = os.getenv("UPS_ACCESS_KEY")
UPS_USERNAME   = os.getenv("UPS_USERNAME")
UPS_PASSWORD   = os.getenv("UPS_PASSWORD")

# Base URL for UPS rate API (use the sandbox/testing endpoint for now)
UPS_RATE_URL = "https://wwwcie.ups.com/rest/Rate"  # test endpoint

def get_rate(shipper, recipient, packages):
    """
    Query UPS Rates API.

    :param shipper: dict with keys: 'Name','AttentionName','Phone','Address':{…}
    :param recipient: same shape as shipper
    :param packages: list of dicts, each with 'Weight' and optionally 'Dimensions'
    :returns: parsed dict of UPS response
    """
    # 1) Build the XML request
    # TODO: fill in build_rate_request_xml(...)
    xml_request = build_rate_request_xml(shipper, recipient, packages)

    # 2) POST it
    resp = requests.post(
        UPS_RATE_URL,
        data=xml_request,
        headers={"Content-Type": "application/xml"}
    )
    resp.raise_for_status()

    # 3) Parse XML → Python dict
    return xmltodict.parse(resp.text)

def build_rate_request_xml(shipper, recipient, packages):
    # AccessRequest as before…
    access_request = f"""
    <?xml version="1.0"?>
    <AccessRequest>
      <AccessLicenseNumber>{UPS_ACCESS_KEY}</AccessLicenseNumber>
      <UserId>{UPS_USERNAME}</UserId>
      <Password>{UPS_PASSWORD}</Password>
    </AccessRequest>
    """

    # Build Shipment element
    # 1) Shipper
    shipper_xml = f"""
    <Shipper>
      <Name>{shipper['Name']}</Name>
      <AttentionName>{shipper['AttentionName']}</AttentionName>
      <PhoneNumber>{shipper['Phone']}</PhoneNumber>
      <ShipperNumber>{UPS_ACCESS_KEY}</ShipperNumber>
      <Address>
        <AddressLine1>{shipper['Address']['AddressLine1']}</AddressLine1>
        <City>{shipper['Address']['City']}</City>
        <StateProvinceCode>{shipper['Address']['StateProvinceCode']}</StateProvinceCode>
        <PostalCode>{shipper['Address']['PostalCode']}</PostalCode>
        <CountryCode>{shipper['Address']['CountryCode']}</CountryCode>
      </Address>
    </Shipper>
    """

    # 2) ShipTo (recipient)
    shipto_xml = f"""
    <ShipTo>
      <Name>{recipient['Name']}</Name>
      <AttentionName>{recipient['AttentionName']}</AttentionName>
      <PhoneNumber>{recipient['Phone']}</PhoneNumber>
      <Address>
        <AddressLine1>{recipient['Address']['AddressLine1']}</AddressLine1>
        <City>{recipient['Address']['City']}</City>
        <StateProvinceCode>{recipient['Address']['StateProvinceCode']}</StateProvinceCode>
        <PostalCode>{recipient['Address']['PostalCode']}</PostalCode>
        <CountryCode>{recipient['Address']['CountryCode']}</CountryCode>
      </Address>
    </ShipTo>
    """

    # 3) Packages
    packages_xml = ""
    for pkg in packages:
        dims = pkg.get("Dimensions", {})
        dim_xml = ""
        if dims:
            dim_xml = f"""
            <Dimensions>
              <UnitOfMeasurement><Code>IN</Code></UnitOfMeasurement>
              <Length>{dims['Length']}</Length>
              <Width>{dims['Width']}</Width>
              <Height>{dims['Height']}</Height>
            </Dimensions>
            """
        packages_xml += f"""
        <Package>
          <PackagingType>
            <Code>02</Code><!-- Customer Supplied Package -->
          </PackagingType>
          {dim_xml}
          <PackageWeight>
            <UnitOfMeasurement><Code>LBS</Code></UnitOfMeasurement>
            <Weight>{pkg['Weight']}</Weight>
          </PackageWeight>
        </Package>
        """

    # Wrap Shipment with those parts
    rate_request = f"""
    <?xml version="1.0"?>
    <RatingServiceSelectionRequest>
      <Request>
        <TransactionReference>
          <CustomerContext>Rate Request</CustomerContext>
        </TransactionReference>
        <RequestAction>Rate</RequestAction>
        <RequestOption>Shop</RequestOption>
      </Request>
      <Shipment>
        {shipper_xml}
        {shipto_xml}
        {packages_xml}
      </Shipment>
    </RatingServiceSelectionRequest>
    """

    # Combine and return
    return access_request + rate_request

    # Combine the two documents in one POST body:
    return access_request + rate_request
