import requests
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import csv

# URL of the official RTE website page where the Tempo calendar is visible.
RTE_WEBPAGE_URL = "https://www.services-rte.com/fr/visualisez-les-donnees-publiees-par-rte/calendrier-des-offres-de-fourniture-de-type-tempo.html"

# URL of the JSON resource used by the official page to get the data.
# The season parameter is filled with the period, in the form "2022-2023".
RTE_API_URL = "https://www.services-rte.com/cms/open_data/v1/tempo?season="

def get_period_for_date(date: datetime) -> str:
    """Returns the period for a given date in the format YYYY-YYYY"""
    year = date.year
    if date.month >= 10:
        return f"{year}-{year + 1}"
    else:
        return f"{year - 1}-{year}"

def get_color_code(color_name: str) -> int:
    """Returns the numeric color code (from 0 to 3) from the color label."""
    color_map = {
        'blue': 1,
        'white': 2,
        'red': 3
    }
    return color_map.get(color_name.lower(), 0)

def fetch_rte_data(period: str) -> Optional[Dict[str, Any]]:
    """Fetches data from the RTE API."""
    url = f"{RTE_API_URL}{period}"
    
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        return data
    except requests.RequestException as e:
        print(f"HTTP request error: {e}")
        return None
    except json.JSONDecodeError as e:
        print(f"JSON decoding error: {e}")
        return None

def export_to_csv(sorted_map: Dict[str, str], filename: str):
    """Exports the sorted map to CSV format."""
    try:
        # Sort keys by date for ordered export
        sorted_keys = sorted(sorted_map.keys())
        
        with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['Date', 'Color', 'Code'])
            
            for date_str in sorted_keys:
                color_name = sorted_map[date_str]
                color_code = get_color_code(color_name)
                writer.writerow([date_str, color_name, color_code])
        
        print(f"Results exported to CSV file: {filename}")
    except Exception as e:
        print(f"Error during CSV export: {e}")

def main(period_arg: Optional[str] = None, export_csv: bool = False):
    """Main function of the command."""
    
    # Determination of the period to query (what we're interested in is tomorrow)
    tomorrow = datetime.now() + timedelta(days=1)
    libPeriode = get_period_for_date(tomorrow)
    
    # Unless the period was provided as a command line argument (special case for retrieving old data)
    if period_arg and period_arg.strip() != '':
        print(f"Forced period: {period_arg}")
        libPeriode = period_arg.strip()
    
    # Querying the RTE server
    print(f"Retrieving data for period: {libPeriode}")
    
    json_data = fetch_rte_data(libPeriode)
    
    if not json_data:
        print("Unable to retrieve data from the RTE API")
        return 1
    
    # Creating a sorted map with date as key and color as value
    sorted_map = {}
    
    # We receive an associative array where the key is the date
    for date_str, color_name in json_data.get('values', {}).items():
        # We ignore keys that are not in the correct format
        if not date_str or not isinstance(date_str, str) or len(date_str) != 10:
            # print(f"We ignore {date_str}")
            continue
            
        try:
            # We verify the date format
            date_obj = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            print(f"Invalid date format, ignored: {date_str}")
            continue
            
        # Adding to the sorted map
        sorted_map[date_str] = color_name
    
    # Sorting the keys of the map by date
    sorted_keys = sorted(sorted_map.keys())
    
    # Displaying the sorted map
    print("Sorted map by date:")
    for key in sorted_keys:
        color_code = get_color_code(sorted_map[key])
        print(f"Date: {key}, Color: {sorted_map[key]} (Code: {color_code})")
    
    # CSV export if requested
    if export_csv:
        csv_filename = f"tempo_data_{libPeriode}.csv"
        export_to_csv(sorted_map, csv_filename)
    
    print("Operation completed without error.")
    return 0

if __name__ == "__main__":
    import sys
    
    # Command line argument handling
    period_arg = None
    export_csv = False
    
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--csv":
            export_csv = True
        elif sys.argv[i] == "--season" and i + 1 < len(sys.argv):
            period_arg = sys.argv[i + 1]
            i += 1
        i += 1
    
    exit_code = main(period_arg, export_csv)
    sys.exit(exit_code)
