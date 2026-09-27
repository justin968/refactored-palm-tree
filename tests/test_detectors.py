from diy_growth.detectors import detect_campaign_scale, detect_search_term_waste
from diy_growth.models import ActionClass, Channel

POLICY = {
    "target_google_roas": 6.5,
    "target_amazon_roas": 10.0,
    "scale_roas_headroom": 1.15,
    "waste_term_spend_threshold_usd": 40,
    "minimum_clicks_for_negative": 8,
    "minimum_conversions_for_scaling": 8,
}

def test_google_waste_term():
    rows=[{"CampaignID":"1","AdgroupID":"2","Searchterm":"irrelevant term","Clicks":12,"Cost":55,"Conversions":0,"ConversionValue":0}]
    ops=detect_search_term_waste(rows,Channel.GOOGLE,POLICY)
    assert len(ops)==1
    assert ops[0].action_class==ActionClass.APPROVAL_REQUIRED

def test_google_scale_candidate():
    rows=[{"CampaignID":"1","Campaignname":"winner","Cost":100,"ConversionValue":900,"Conversions":10,"dailybudget":100}]
    ops=detect_campaign_scale(rows,Channel.GOOGLE,POLICY)
    assert len(ops)==1
    assert ops[0].evidence["proposed_budget"]==110
