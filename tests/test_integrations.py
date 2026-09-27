from diy_growth.integrations.supermetrics import SupermetricsQuery

def test_supermetrics_query_payload():
    q = SupermetricsQuery(
        ds_id="AW",
        account_id="123",
        fields=["Date","Clicks","Cost"],
        start_date="2026-09-01",
        end_date="2026-09-07",
        settings={"report_type":"Campaign"},
    )
    p = q.payload()
    assert p["ds_id"] == "AW"
    assert p["ds_accounts"] == "123"
    assert p["fields"] == "Date,Clicks,Cost"
    assert p["settings"]["report_type"] == "Campaign"
