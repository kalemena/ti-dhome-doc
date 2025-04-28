#!/bin/bash

# Victoria Metrics - Render JSON report as HTML
# Usage: ./01-render-metrics.sh <json-report> [output-html]

set -e

JSON_REPORT="${1:-metrics-report.json}"
OUTPUT_HTML="${2:-metrics-report.html}"

if [ ! -f "$JSON_REPORT" ]; then
    echo "Error: JSON report file '$JSON_REPORT' not found"
    echo "Please run ./01-list-metrics.sh first to generate the report"
    exit 1
fi

echo "Rendering HTML report from: $JSON_REPORT"
echo "Output will be saved to: $OUTPUT_HTML"

# Generate HTML report
cat > "$OUTPUT_HTML" << 'EOF'
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Victoria Metrics Analysis Report</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            color: #333;
            background: #f5f5f5;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        
        .header {
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            margin-bottom: 30px;
        }
        
        .header h1 {
            color: #2c3e50;
            margin-bottom: 10px;
        }
        
        .meta-info {
            color: #666;
            font-size: 14px;
        }
        
        .summary-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        
        .summary-card {
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        
        .summary-card h3 {
            color: #2c3e50;
            margin-bottom: 10px;
            font-size: 16px;
        }
        
        .summary-value {
            font-size: 24px;
            font-weight: bold;
            color: #3498db;
        }
        
        .summary-label {
            font-size: 12px;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .section {
            background: white;
            margin-bottom: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            overflow: hidden;
        }
        
        .section-header {
            background: #34495e;
            color: white;
            padding: 20px;
            font-size: 18px;
            font-weight: bold;
        }
        
        .section-content {
            padding: 20px;
        }
        
        .top-metrics {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
            gap: 20px;
        }
        
        .metric-list {
            background: #f8f9fa;
            border-radius: 6px;
            padding: 15px;
        }
        
        .metric-list h4 {
            color: #2c3e50;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #3498db;
        }
        
        .metric-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 0;
            border-bottom: 1px solid #eee;
        }
        
        .metric-item:last-child {
            border-bottom: none;
        }
        
        .metric-name {
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 12px;
            color: #2c3e50;
            flex: 1;
            margin-right: 10px;
        }
        
        .metric-value {
            font-weight: bold;
            color: #e74c3c;
            font-size: 12px;
        }
        
        .controls {
            margin-bottom: 20px;
            padding: 15px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        
        .search-box {
            width: 100%;
            padding: 10px;
            border: 2px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
        }
        
        .search-box:focus {
            outline: none;
            border-color: #3498db;
        }
        
        .metrics-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
        }
        
        .metrics-table th {
            background: #34495e;
            color: white;
            padding: 12px 8px;
            text-align: left;
            font-weight: bold;
            position: sticky;
            top: 0;
        }
        
        .metrics-table td {
            padding: 8px;
            border-bottom: 1px solid #eee;
        }
        
        .metrics-table tr:hover {
            background: #f8f9fa;
        }
        
        .metric-name-cell {
            font-family: 'Monaco', 'Menlo', monospace;
            font-weight: bold;
            color: #2c3e50;
        }
        
        .number-cell {
            text-align: right;
            font-weight: bold;
        }
        
        .date-cell {
            font-size: 11px;
            color: #666;
        }
        
        .labels-cell {
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 10px;
            color: #666;
            max-width: 200px;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        
        .no-data {
            color: #999;
            font-style: italic;
        }
        
        .footer {
            text-align: center;
            padding: 20px;
            color: #666;
            font-size: 12px;
        }
        
        @media (max-width: 768px) {
            .container {
                padding: 10px;
            }
            
            .summary-grid {
                grid-template-columns: 1fr;
            }
            
            .top-metrics {
                grid-template-columns: 1fr;
            }
            
            .metrics-table {
                font-size: 10px;
            }
            
            .metrics-table th,
            .metrics-table td {
                padding: 6px 4px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Victoria Metrics Analysis Report</h1>
            <div class="meta-info">
                <div>Instance: <span id="vm-instance"></span></div>
                <div>Generated: <span id="analysis-timestamp"></span></div>
                <div>Total Metrics: <span id="total-metrics"></span></div>
            </div>
        </div>
        
        <div class="summary-grid">
            <div class="summary-card">
                <div class="summary-label">Total Series</div>
                <div class="summary-value" id="total-series">-</div>
            </div>
            <div class="summary-card">
                <div class="summary-label">Metrics with Data</div>
                <div class="summary-value" id="metrics-with-data">-</div>
            </div>
            <div class="summary-card">
                <div class="summary-label">Est. Data Points (24h)</div>
                <div class="summary-value" id="total-points">-</div>
            </div>
            <div class="summary-card">
                <div class="summary-label">Est. Size (MB)</div>
                <div class="summary-value" id="total-size">-</div>
            </div>
            <div class="summary-card">
                <div class="summary-label">Oldest Data</div>
                <div class="summary-value" id="oldest-data">-</div>
            </div>
            <div class="summary-card">
                <div class="summary-label">Newest Data</div>
                <div class="summary-value" id="newest-data">-</div>
            </div>
            <div class="summary-card">
                <div class="summary-label">Avg Retention (Days)</div>
                <div class="summary-value" id="avg-retention">-</div>
            </div>
        </div>
        
        <div class="section">
            <div class="section-header">Top Metrics</div>
            <div class="section-content">
                <div class="top-metrics">
                    <div class="metric-list">
                        <h4>Top 10 by Series Count</h4>
                        <div id="top-by-series"></div>
                    </div>
                    <div class="metric-list">
                        <h4>Top 10 by Size (MB)</h4>
                        <div id="top-by-size"></div>
                    </div>
                    <div class="metric-list">
                        <h4>Top 10 by Retention (Days)</h4>
                        <div id="top-by-retention"></div>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="section">
            <div class="section-header">All Metrics</div>
            <div class="section-content">
                <div class="controls">
                    <input type="text" id="search" class="search-box" placeholder="Search metrics by name...">
                </div>
                <div style="overflow-x: auto;">
                    <table class="metrics-table">
                        <thead>
                            <tr>
                                <th>Metric Name</th>
                                <th>Series</th>
                                <th>Est. Points (24h)</th>
                                <th>Est. Size (MB)</th>
                                <th>First Data</th>
                                <th>Last Data</th>
                                <th>Retention (Days)</th>
                                <th>Labels</th>
                            </tr>
                        </thead>
                        <tbody id="metrics-tbody">
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
        
        <div class="footer">
            Generated by Victoria Metrics Migration Toolkit
        </div>
    </div>

    <script>
        // Load and render the JSON data
        const jsonData = 
EOF

# Append the JSON data to the HTML file
jq '.' "$JSON_REPORT" >> "$OUTPUT_HTML"

# Continue with the JavaScript and closing HTML
cat >> "$OUTPUT_HTML" << 'EOF'
        ;

        function formatNumber(num) {
            if (num === null || num === undefined) return '-';
            return num.toLocaleString();
        }

        function formatDate(dateStr) {
            if (!dateStr || dateStr === 'null') return '-';
            return new Date(dateStr).toLocaleDateString() + ' ' + new Date(dateStr).toLocaleTimeString();
        }

        function formatLabels(labels) {
            if (!labels || labels.length === 0) return '-';
            return labels.join(', ');
        }

        function renderTopMetrics(containerId, metrics, valueKey, suffix = '') {
            const container = document.getElementById(containerId);
            if (!metrics || metrics.length === 0) {
                container.innerHTML = '<div class="no-data">No data available</div>';
                return;
            }
            
            container.innerHTML = metrics.map(metric => `
                <div class="metric-item">
                    <div class="metric-name">${metric.name}</div>
                    <div class="metric-value">${formatNumber(metric[valueKey])}${suffix}</div>
                </div>
            `).join('');
        }

        function renderMetricsTable() {
            const tbody = document.getElementById('metrics-tbody');
            const searchInput = document.getElementById('search');
            
            function filterAndRender() {
                const searchTerm = searchInput.value.toLowerCase();
                const filteredMetrics = jsonData.metrics.filter(metric => 
                    metric.name.toLowerCase().includes(searchTerm)
                );
                
                tbody.innerHTML = filteredMetrics.map(metric => `
                    <tr>
                        <td class="metric-name-cell">${metric.name}</td>
                        <td class="number-cell">${formatNumber(metric.series_count)}</td>
                        <td class="number-cell">${formatNumber(metric.approx_data_points_24h)}</td>
                        <td class="number-cell">${formatNumber(metric.estimated_size_mb)}</td>
                        <td class="date-cell">${formatDate(metric.first_date)}</td>
                        <td class="date-cell">${formatDate(metric.last_date)}</td>
                        <td class="number-cell">${metric.data_retention_days ? formatNumber(metric.data_retention_days) : '-'}</td>
                        <td class="labels-cell">${formatLabels(metric.labels)}</td>
                    </tr>
                `).join('');
            }
            
            searchInput.addEventListener('input', filterAndRender);
            filterAndRender();
        }

        // Populate the page
        document.getElementById('vm-instance').textContent = jsonData.vm_instance || '-';
        document.getElementById('analysis-timestamp').textContent = formatDate(jsonData.analysis_timestamp);
        document.getElementById('total-metrics').textContent = formatNumber(jsonData.total_metrics);

        if (jsonData.summary) {
            document.getElementById('total-series').textContent = formatNumber(jsonData.summary.total_series);
            document.getElementById('metrics-with-data').textContent = formatNumber(jsonData.summary.metrics_with_data);
            document.getElementById('total-points').textContent = formatNumber(jsonData.summary.total_estimated_points_24h);
            document.getElementById('total-size').textContent = formatNumber(jsonData.summary.total_estimated_size_mb);
            document.getElementById('oldest-data').textContent = formatDate(jsonData.summary.oldest_data_date);
            document.getElementById('newest-data').textContent = formatDate(jsonData.summary.newest_data_date);
            document.getElementById('avg-retention').textContent = jsonData.summary.avg_retention_days ? 
                Math.floor(jsonData.summary.avg_retention_days) : '-';

            renderTopMetrics('top-by-series', jsonData.summary.top_10_by_series, 'series_count');
            renderTopMetrics('top-by-size', jsonData.summary.top_10_by_size, 'estimated_size_mb', ' MB');
            renderTopMetrics('top-by-retention', jsonData.summary.top_10_by_retention, 'data_retention_days', ' days');
        }

        renderMetricsTable();
    </script>
</body>
</html>
EOF

echo "HTML report generated successfully: $OUTPUT_HTML"
echo "Open the file in your browser to view the interactive report"