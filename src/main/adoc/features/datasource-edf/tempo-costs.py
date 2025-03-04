import requests
import json
from datetime import datetime

def get_tarifs():
    
    GOUV_URL = "https://tabular-api.data.gouv.fr/api/resources/0c3d1d36-c412-4620-8566-e5cbb4fa2b5a/data/?page_size=1&P_SOUSCRITE__exact=6&__id__sort=desc"
    
    try:
        # Fetch data from API
        response = requests.get(GOUV_URL, timeout=10)
        response.raise_for_status()  # Raises an HTTPError for bad responses
        
        data = response.json()
        
        # Check if we have data
        if not data or 'data' not in data or len(data['data']) == 0:
            print("ERROR: No tariff data available")
            return 1

        tarif_gouv = data['data'][0]
        id_tarif = int(tarif_gouv['__id'])
        
        # Check if tariff is expired
        if tarif_gouv['DATE_FIN'] is not None and tarif_gouv['DATE_FIN'] < datetime.now().strftime('%Y-%m-%d'):
            print(f"ERROR: Tariff with ID {id_tarif} is expired")
            return 1

        # Print the tariff data
        print("SUCCESS: Tariff data retrieved:")
        print(f"ID: {id_tarif}")
        print(f"Start Date: {tarif_gouv.get('DATE_DEBUT', 'N/A')}")
        print(f"Blue HC: {tarif_gouv.get('PART_VARIABLE_HCBleu_TTC', 'N/A')}")
        print(f"Blue HP: {tarif_gouv.get('PART_VARIABLE_HPBleu_TTC', 'N/A')}")
        print(f"White HC: {tarif_gouv.get('PART_VARIABLE_HCBlanc_TTC', 'N/A')}")
        print(f"White HP: {tarif_gouv.get('PART_VARIABLE_HPBlanc_TTC', 'N/A')}")
        print(f"Red HC: {tarif_gouv.get('PART_VARIABLE_HCRouge_TTC', 'N/A')}")
        print(f"Red HP: {tarif_gouv.get('PART_VARIABLE_HPRouge_TTC', 'N/A')}")
        
        return 0
        
    except requests.RequestException as e:
        print(f"ERROR: Failed to fetch tariff data - {str(e)}")
        return 1
    except json.JSONDecodeError as e:
        print(f"ERROR: Failed to decode JSON - {str(e)}")
        return 1
    except Exception as e:
        print(f"ERROR: Unexpected error - {str(e)}")
        return 1

if __name__ == "__main__":
    get_tarifs()