from diy_growth.models import Channel, UnitEconomics

def test_unit_economics():
    e = UnitEconomics(sku="KOH-200", channel=Channel.DIY, selling_price=568, cogs=250, packaging=30, outbound_shipping=60, payment_fee=17)
    assert e.contribution_before_ads == 211
    assert e.break_even_cac == 211
    assert round(e.break_even_roas, 3) == round(568/211, 3)
