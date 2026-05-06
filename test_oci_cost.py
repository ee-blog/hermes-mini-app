#!/home/ubuntu/.hermes/oci-venv/bin/python3
"""Test OCI Cost API"""
import oci
from datetime import datetime, timezone

config = oci.config.from_file('~/.oci/config')
tenancy = config['tenancy']

usage_client = oci.usage_api.UsageapiClient(config)

today = datetime.now(timezone.utc)
# For MONTHLY granularity, dates must be at midnight (hour=0, min=0, sec=0)
start_of_month = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
end_of_month = today.replace(hour=0, minute=0, second=0, microsecond=0)

query = oci.usage_api.models.RequestSummarizedUsagesDetails(
    tenant_id=tenancy,
    time_usage_started=start_of_month.strftime('%Y-%m-%dT%H:%M:%SZ'),
    time_usage_ended=end_of_month.strftime('%Y-%m-%dT%H:%M:%SZ'),
    granularity='MONTHLY',
    group_by=['service'],
)

try:
    result = usage_client.request_summarized_usages(query)
    items = result.data.items
    print(f'Items: {len(items)}')
    total = 0
    for item in items:
        total += item.computed_amount
        svc = item.service or 'Unknown'
        amt = item.computed_amount
        cur = item.currency or 'USD'
        print(f'  {svc:30s} {amt:>10.5f} {cur}')
    print('-' * 45)
    print(f'  {"TOTAL":30s} {total:>10.5f} USD')
except oci.exceptions.ServiceError as e:
    print(f'Error: {e.status} {e.code}')
    print(e.message)
except Exception as e:
    import traceback
    traceback.print_exc()
