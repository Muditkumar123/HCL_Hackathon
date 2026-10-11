"""Small CSVs in the TASK-001 trap shape, shared by the backend tests."""

ORDERS = """order_id,product,qty,destination,due,priority
O-1,P1,100,US,2026-10-20,high
O-2,P3,100,DE,2026-10-20,normal
O-3,P2,200,FR,2026-10-20,normal
O-4,P2,300,DE,2026-10-20,normal
"""
PLANTS = """plant,product,weekly_capacity,unit_cost,energy_kwh_per_unit,carbon_kg_per_kwh
A,P1,1000,10,2,0.5
A,P2,1000,20,2,0.5
B,P1,800,12,2,0.6
B,P2,600,15,2,0.6
C,P1,150,13,2,0.05
C,P3,150,30,1,0.05
"""
TRANSPORT = "plant,destination,transport_cost_per_unit,transit_days,duty_pct,carbon_kg_per_unit\n" + "".join(
    f"{p},{d},1.0,{25 if (p, d) == ('A', 'US') else 3},{40 if (p, d) == ('B', 'FR') else 0},0.1\n"
    for p in "ABC" for d in ("US", "DE", "FR"))
EVENTS = "event_id,plant,from_date,to_date,capacity_loss_pct\nE-1,B,2026-10-12,2026-10-18,30\n"
CSVS = {"plants": PLANTS, "transport_duties": TRANSPORT, "orders": ORDERS, "capacity_events": EVENTS}
