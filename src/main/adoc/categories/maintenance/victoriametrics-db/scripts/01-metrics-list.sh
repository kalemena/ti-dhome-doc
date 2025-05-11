#!/bin/bash

# Victoria Metrics - List all metrics with statistics
# Usage: ./01-list-metrics.sh <vm-url> [output-file] [filter-file]

set -e

VM_URL="http://victoriamertics:8428"
OUTPUT_FILE="metrics-report.json"
# FILTER_FILE="metrics-filter.yaml"

usage() {
    echo "Usage: $0 [-u <vm-url>] [-o <output-file>] [-f <filter-file>]"
    echo "  -u: Victoria Metrics URL (default: http://victoriamertics:8428)"
    echo "  -o: Output JSON report file (default: metrics-report.json)"
    echo "  -f: Path to filter patterns YAML file"
    exit 1
}

while getopts "u:o:f:h" opt; do
    case "$opt" in
        u) VM_URL="$OPTARG" ;;
        o) OUTPUT_FILE="$OPTARG" ;;
        f) FILTER_FILE="$OPTARG" ;;
        h|*) usage ;;
    esac
done

TEMP_DIR="/tmp/vm-migration"

mkdir -p "$TEMP_DIR"

echo "Analyzing Victoria Metrics instance at: $VM_URL"
if [ -n "$FILTER_FILE" ]; then
    echo "Filter patterns file: $FILTER_FILE"
else
    echo "No filter file specified - processing all metrics"
fi
echo "Output will be saved to: $OUTPUT_FILE"

# Check if filter file exists and filter patterns are provided
FILTER_PATTERNS=""
if [ -n "$FILTER_FILE" ]; then
    if [ ! -f "$FILTER_FILE" ]; then
        echo "Warning: Filter file '$FILTER_FILE' not found. Running without filtering."
    else
        echo "Loading filter patterns from: $FILTER_FILE"
        # Extract patterns from YAML file using grep and sed
        # This extracts lines after "patterns:" that start with "- "
        FILTER_PATTERNS=$(grep -E '^\s*-\s*"' "$FILTER_FILE" | sed 's/.*"\([^"]*\)".*/\1/' | tr '\n' '|' | sed 's/|$//')
        
        if [ -z "$FILTER_PATTERNS" ]; then
            echo "Warning: No patterns found in filter file. Running without filtering."
            FILTER_PATTERNS=""
        else
            echo "Using filter patterns: $FILTER_PATTERNS"
        fi
    fi
else
    echo "No filter file provided - will process all metrics"
fi

# Get all metric names
echo "Fetching all metric names..."
curl -s "${VM_URL}/api/v1/label/__name__/values" | jq -r '.data[]' > "$TEMP_DIR/all_metrics.txt"

TOTAL_METRICS=$(wc -l < "$TEMP_DIR/all_metrics.txt")
echo "Found $TOTAL_METRICS unique metrics"

# Filter metrics if patterns are provided
if [ -n "$FILTER_PATTERNS" ]; then
    echo "Filtering metrics with pattern: $FILTER_PATTERNS"
    grep -E "$FILTER_PATTERNS" "$TEMP_DIR/all_metrics.txt" > "$TEMP_DIR/filtered_metrics.txt" || true
    FILTERED_COUNT=$(wc -l < "$TEMP_DIR/filtered_metrics.txt")
    echo "Filtered to $FILTERED_COUNT metrics matching the patterns"
    mv "$TEMP_DIR/filtered_metrics.txt" "$TEMP_DIR/all_metrics.txt"
    TOTAL_METRICS=$FILTERED_COUNT
else
    echo "No filtering applied - analyzing all $TOTAL_METRICS metrics"
fi

# Initialize report
cat > "$OUTPUT_FILE" << 'EOF'
{
  "analysis_timestamp": "",
  "vm_instance": "",
  "total_metrics": 0,
  "metrics": []
}
EOF

# Update basic info
jq --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
   --arg vm_url "$VM_URL" \
   --argjson total "$TOTAL_METRICS" \
   '.analysis_timestamp = $timestamp | .vm_instance = $vm_url | .total_metrics = $total' \
   "$OUTPUT_FILE" > "$TEMP_DIR/report_temp.json" && mv "$TEMP_DIR/report_temp.json" "$OUTPUT_FILE"

echo "Analyzing each metric (this may take a while)..."

counter=0
while IFS= read -r metric; do
    counter=$((counter + 1))
    echo "Processing metric $counter/$TOTAL_METRICS: $metric"
    
    # Get metric info for last 24h
    end_time=$(date +%s)
    start_time=$((end_time - 86400))  # 24 hours ago
    
    # Query metric data points count
    query_url="${VM_URL}/api/v1/query_range"
    query_params="query=${metric}&start=${start_time}&end=${end_time}&step=5s"
    
    response=$(curl -s "${query_url}?${query_params}" || echo '{"data":{"result":[]}}')
    
    # Extract series count and count actual data points
    series_count=$(echo "$response" | jq '.data.result | length')
    
    echo "  Series count for $metric: $series_count"
    
    # Count actual data points from the response
    # Each series has a "values" array with [timestamp, value] pairs
    total_data_points=$(echo "$response" | jq '([.data.result[].values | length] | add // 0) / 2 | floor')
    
    echo "  Actual data points in response: $total_data_points"
    
    # Calculate approximate data points for 24h (estimate based on what we got)
    # If we got data, estimate what 24h would look like
    if [ "$total_data_points" -gt 0 ]; then
        # Get the time range covered by the data
        first_ts=$(echo "$response" | jq -r '[.data.result[].values[0][0]] | min')
        last_ts=$(echo "$response" | jq -r '[.data.result[].values[-1][0]] | max')
        
        if [ -n "$first_ts" ] && [ "$first_ts" != "null" ]; then
            time_range_seconds=$((last_ts - first_ts))
            if [ "$time_range_seconds" -gt 0 ]; then
                # Estimate total points for full 24h (86400 seconds)
                approx_points=$((total_data_points * 86400 / time_range_seconds))
            else
                approx_points=$total_data_points
            fi
        else
            approx_points=$total_data_points
        fi
    else
        approx_points=0
    fi
    
    echo "  Estimated points for 24h: $approx_points"
    
    # Get first and last timestamps for this metric
    # Query for the earliest data point
    first_timestamp=""
    last_timestamp=""
    
    if [ "$series_count" -gt 0 ]; then
        # Get the earliest timestamp - search back 5 years to be safe
        early_start=$((end_time - 157680000))  # 5 years ago (5 * 365 * 24 * 3600)
        
        # Use /api/v1/series to get the actual time range for this metric
        series_response=$(curl -s "${VM_URL}/api/v1/series?match[]=${metric}" || echo '{"data":[]}')
        
        # If we have series data, query for the actual first timestamp
        if echo "$series_response" | jq -e '.data | length > 0' >/dev/null 2>&1; then
            # Query with a large step to get sparse data points and find the earliest
            first_response=$(curl -s "${VM_URL}/api/v1/query_range?query=${metric}&start=${early_start}&end=${end_time}&step=86400s" || echo '{"data":{"result":[]}}')
            
            # Extract first timestamp from all series and get the absolute minimum
            first_timestamp=$(echo "$first_response" | jq -r '
                [.data.result[]?.values[]?[0] // empty] | 
                map(tonumber) | 
                sort | 
                .[0] // empty
            ')
            
            # If we didn't get a result with daily steps, try with smaller steps on a shorter range
            if [ -z "$first_timestamp" ] || [ "$first_timestamp" = "null" ]; then
                # Try last 6 months with hourly steps
                recent_start=$((end_time - 15552000))  # 6 months ago
                first_response=$(curl -s "${VM_URL}/api/v1/query_range?query=${metric}&start=${recent_start}&end=${end_time}&step=3600s" || echo '{"data":{"result":[]}}')
                first_timestamp=$(echo "$first_response" | jq -r '
                    [.data.result[]?.values[]?[0] // empty] | 
                    map(tonumber) | 
                    sort | 
                    .[0] // empty
                ')
            fi
        fi
        
        # Get the latest timestamp
        latest_response=$(curl -s "${VM_URL}/api/v1/query?query=${metric}" || echo '{"data":{"result":[]}}')
        last_timestamp=$(echo "$latest_response" | jq -r '
            [.data.result[]?.value[0] // empty] | 
            map(tonumber) | 
            sort | 
            .[-1] // empty
        ')
    fi
    
    # Convert timestamps to human readable format
    first_date=""
    last_date=""
    data_retention_days=""
    
    if [ -n "$first_timestamp" ] && [ "$first_timestamp" != "null" ] && [ "$first_timestamp" != "" ]; then
        # Handle both integer and float timestamps
        first_ts_int=$(echo "$first_timestamp" | cut -d. -f1)
        first_date=$(date -r "$first_ts_int" -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "")
        
        if [ -n "$last_timestamp" ] && [ "$last_timestamp" != "null" ] && [ "$last_timestamp" != "" ]; then
            last_ts_int=$(echo "$last_timestamp" | cut -d. -f1)
            last_date=$(date -r "$last_ts_int" -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "")
            data_retention_days=$(( (last_ts_int - first_ts_int) / 86400 ))
        fi
    fi
    
    # Get sample labels for the metric
    labels_response=$(curl -s "${VM_URL}/api/v1/series?match[]=${metric}&start=${start_time}&end=${end_time}" || echo '{"data":[]}')
    unique_labels=$(echo "$labels_response" | jq '[.data[0] // {} | keys] | unique')
    
    # Calculate estimated size in MB
    # Victoria Metrics uses efficient compression (~1-2 bytes per data point on average)
    # We use 2 bytes as a conservative estimate for compressed storage
    
    # Calculate full retention size based on actual data retention period
    if [ -n "$data_retention_days" ] && [ "$data_retention_days" -gt 0 ]; then
        # Scale 24h estimate to full retention period
        full_approx_points=$((approx_points * data_retention_days))
        # Calculate size for full retention period
        estimated_size_mb=$(echo "$full_approx_points" | jq '(. / 1048576) | floor')
        echo "  Estimated size (full retention): $estimated_size_mb MB for ${data_retention_days} days"
    else
        # No retention data, use 24h estimate
        full_approx_points=$approx_points
        estimated_size_mb=$(echo "$approx_points" | jq '(. * 2 / 1048576) | floor')
        echo "  Estimated size (24h): $estimated_size_mb MB"
    fi
    
    # Add metric info to report
    metric_info=$(jq -n \
        --arg name "$metric" \
        --argjson series_count "$series_count" \
        --argjson total_data_points "$total_data_points" \
        --argjson approx_points "$approx_points" \
        --argjson full_approx_points "$full_approx_points" \
        --argjson estimated_size "$estimated_size_mb" \
        --argjson labels "$unique_labels" \
        --arg first_timestamp "${first_timestamp:-null}" \
        --arg last_timestamp "${last_timestamp:-null}" \
        --arg first_date "${first_date:-null}" \
        --arg last_date "${last_date:-null}" \
        --arg retention_days "${data_retention_days:-null}" \
        '{
            name: $name,
            series_count: $series_count,
            actual_data_points_retrieved: $total_data_points,
            approx_data_points_24h: $approx_points,
            approx_data_points_full_retention: $full_approx_points,
            labels: $labels,
            estimated_size_mb: $estimated_size,
            estimated_size_24h_mb: (if $retention_days == "null" or $retention_days == 0 then $estimated_size else (($approx_points * 2 / 1048576) | floor) end),
            first_timestamp: (if $first_timestamp == "null" then null else ($first_timestamp | tonumber) end),
            last_timestamp: (if $last_timestamp == "null" then null else ($last_timestamp | tonumber) end),
            first_date: (if $first_date == "null" then null else $first_date end),
            last_date: (if $last_date == "null" then null else $last_date end),
            data_retention_days: (if $retention_days == "null" then null else ($retention_days | tonumber) end)
        }')
    
    # Append to report
    jq --argjson metric "$metric_info" '.metrics += [$metric]' "$OUTPUT_FILE" > "$TEMP_DIR/report_temp.json" && mv "$TEMP_DIR/report_temp.json" "$OUTPUT_FILE"
    
done < "$TEMP_DIR/all_metrics.txt"

# Generate summary statistics
jq '.summary = {
    total_series: ([.metrics[].series_count] | add // 0),
    total_estimated_points_24h: ([.metrics[].approx_data_points_24h] | add // 0),
    total_estimated_points_full_retention: ([.metrics[].approx_data_points_full_retention] | add // 0),
    total_estimated_size_mb: ([.metrics[].estimated_size_mb // 0] | add),
    total_estimated_size_24h_mb: ([.metrics[].estimated_size_24h_mb // 0] | add),
    metrics_with_data: ([.metrics[] | select(.series_count > 0)] | length),
    oldest_data_date: ([.metrics[].first_date | select(. != null)] | min // "N/A"),
    newest_data_date: ([.metrics[].last_date | select(. != null)] | max // "N/A"),
    avg_retention_days: ([.metrics[].data_retention_days | select(. != null)] | if length > 0 then add / length else 0 end),
    top_10_by_series: (.metrics | sort_by(.series_count) | reverse | .[0:10]),
    top_10_by_size: (.metrics | sort_by(.estimated_size_mb // 0) | reverse | .[0:10]),
    top_10_by_retention: (.metrics | sort_by(.data_retention_days // 0) | reverse | .[0:10])
}' "$OUTPUT_FILE" > "$TEMP_DIR/report_temp.json" && mv "$TEMP_DIR/report_temp.json" "$OUTPUT_FILE"

echo "Analysis complete! Report saved to: $OUTPUT_FILE"
echo "Summary:"
jq -r '.summary | "Total metrics: \(.total_series // 0)
Metrics with data: \(.metrics_with_data // 0)
Estimated data points (24h): \(.total_estimated_points_24h // 0)
Estimated data points (full retention): \(.total_estimated_points_full_retention // 0)
Estimated size (24h): \(.total_estimated_size_24h_mb // 0) MB
Estimated size (full retention): \(.total_estimated_size_mb // 0) MB
Oldest data: \(.oldest_data_date // "N/A")
Newest data: \(.newest_data_date // "N/A")
Average retention: \(.avg_retention_days // 0 | floor) days"' "$OUTPUT_FILE"

# Cleanup
rm -rf "$TEMP_DIR"