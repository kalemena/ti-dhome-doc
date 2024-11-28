import requests

prometheus_url = "http://localhost:8428/api/v1/query"
query_template_solar = 'sum_over_time(increase(opendtu_YieldTotal{{type="AC"}}[1h] offset {}h)[365d:1d])'
query_template_to_grid = 'sum_over_time(increase(zigbee_energy_produced_b{{location="C03~Garage~PowerMeter"}}[1h] offset {}h)[365d:1d])'
query_template_from_grid = 'sum_over_time(increase(zigbee_energy_b{{location="C03~Garage~PowerMeter"}}[1h] offset {}h)[365d:1d])'

def fetch_query(query_template):
    for hour in range(24):
        query = query_template.format(24-hour)
        # print(f"Query: {query}")
        response = requests.get(prometheus_url, params={'query': query})
        data = response.json()
        
        if data['status'] == 'success':
            result = data['data']['result']
            if result:
                value = result[0]['value'][1]
                try:
                    value = round(float(value), 2)
                    print(f"{hour:02}:00, {value}")
                except ValueError:
                    print(f"{hour:02}:00, '{value}' is not a valid integer.")
            else:
                print(f"{hour:02}:00, No results found.")
        else:
            print(f"{hour:02}:00, Query failed with status '{data['status']}'.")

print(f"\nTime, Solar")
fetch_query(query_template_solar)

print(f"\nTime, ToGrid")
fetch_query(query_template_to_grid)

print(f"\nTime, FromGrid")
fetch_query(query_template_from_grid)