import requests

prometheus_url = "http://localhost:8428/api/v1/query"

# metrics to measure
metrics = [
    'opendtu_YieldTotal{type="AC"}',
    'sensors_zigbee_energy_produced_b{location="C03~Garage~PowerMeter"}',
    'sensors_zigbee_energy_b{location="C03~Garage~PowerMeter"}'
]

query_template = 'sum_over_time(increase({}[1h] offset {}h)[365d:1d])'

def fetch_query(query_template, metric):
    for hour in range(24):
        query = query_template.format(metric, 24-hour)
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

for metric in metrics:
    print(f"\nTime, Value")
    fetch_query(query_template, metric)

