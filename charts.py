import dash
from dash import dcc, html, dash_table
import dash_bootstrap_components as dbc
import pandas as pd
import requests
from dash.dependencies import Input, Output, State, ALL
import plotly.express as px
import plotly.graph_objects as go
from flask_caching import Cache

# Step 1: Fetch Data from the API
def fetch_data():
    url = "http://2.24.1.7:5002/api/logs"
    response = requests.get(url)
    data = response.json()
    df = pd.DataFrame(data)
    
    # Convert necessary columns to numeric, coercing errors
    numeric_columns = ['orders', 'units', 'fulfilled', 'uph', 'found', 'lates', 'inf', 'cancelled']
    for col in numeric_columns:
        df[col] = pd.to_numeric(df[col].str.replace('%', ''), errors='coerce')
    
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    
    # Extract date part for daily aggregation
    df['date'] = df['timestamp'].dt.date
    
    return df

# Step 2: Create Dash App with Bootstrap theme and enable suppress_callback_exceptions
external_stylesheets = [
    dbc.themes.CERULEAN,  # A light, blue-themed Bootstrap stylesheet
    'https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap',
    '/assets/custom.css'  # Make sure to place your custom.css in the `assets/` folder
]

app = dash.Dash(__name__, external_stylesheets=external_stylesheets, suppress_callback_exceptions=True)

# Initialize the cache
cache = Cache(app.server, config={
    'CACHE_TYPE': 'simple',
    'CACHE_DEFAULT_TIMEOUT': 300  # Cache timeout in seconds
})

@cache.memoize()
def fetch_data_cached():
    return fetch_data()

# Step 3: Define Layout with Tabs and Modal
app.layout = dbc.Container([
    dbc.NavbarSimple(
        brand="Amazon Daily Update",
        color="primary",
        dark=False,
        className="mb-4",
    ),
    dcc.Tabs(id="tabs-example", value='tab-summary', children=[
        dcc.Tab(label='Summary & Charts', value='tab-summary'),
        dcc.Tab(label='Logs Table', value='tab-table'),
    ], className='custom-tabs'),
    html.Div(id='tabs-content-example'),
    # Modal for Store Selection
    dbc.Modal([
        dbc.ModalHeader("Select Stores"),
        dbc.ModalBody([
            dcc.Input(
                id="store-search", 
                type="text", 
                placeholder="Search stores...", 
                className="mb-2", 
                style={"width": "100%"}
            ),
            dbc.Button("Select All", id="select-all-stores", className="mb-2"),
            dbc.Button("Select None", id="select-none-stores", className="mb-2", style={"margin-left": "10px"}),
            dcc.Checklist(
                id='store-checklist',
                options=[],
                value=[],
                labelStyle={'display': 'block', 'color': '#000'}
            ),
            html.Div(id='store-presets', children=[]),  # Presets area
            dbc.Button("Save Current Selection as Preset", id="save-preset", className="mt-2 btn-primary"),
        ]),
        dbc.ModalFooter([
            dbc.Button("Apply", id="apply-store-selection", className="ml-auto btn-success"),
            dbc.Button("Close", id="close-store-modal", className="ml-2 btn-secondary")
        ])
    ], id="store-selection-modal", size="lg"),
], fluid=True, className="custom-container")

# Callback to update Layout Based on API Data and Handle Store Selection
@app.callback(
    Output('store-dropdown', 'options'),
    Output('store-checklist', 'options'),
    Output('store-checklist', 'value'),
    Output('store-selection-modal', 'is_open'),
    Input('date-picker-1', 'start_date'),
    Input('date-picker-1', 'end_date'),
    Input('open-store-modal', 'n_clicks'),
    Input('apply-store-selection', 'n_clicks'),
    Input('select-all-stores', 'n_clicks'),
    Input('select-none-stores', 'n_clicks'),
    Input('store-search', 'value'),
    State('store-checklist', 'value'),
    State('store-dropdown', 'options'),
    State('store-selection-modal', 'is_open'),
    prevent_initial_call=True
)
def update_store_selection(start_date, end_date, open_click, apply_click, select_all_click, select_none_click, search_value, checklist_values, store_options, is_modal_open):
    ctx = dash.callback_context

    df = fetch_data_cached()

    # Filter by date range
    if start_date and end_date:
        df = df[(df['timestamp'] >= start_date) & (df['timestamp'] <= end_date)]

    # Filter store options based on search value
    store_options = [{"label": store, "value": store} for store in df['store'].unique()]
    if search_value:
        store_options = [option for option in store_options if search_value.lower() in option['label'].lower()]

    # Handle store selection modal actions
    if ctx.triggered:
        triggered_id = ctx.triggered[0]['prop_id'].split('.')[0]

        if triggered_id == 'open-store-modal':
            is_modal_open = not is_modal_open
        elif triggered_id == 'apply-store-selection':
            is_modal_open = False
        elif triggered_id == 'select-all-stores':
            checklist_values = [option['value'] for option in store_options]
        elif triggered_id == 'select-none-stores':
            checklist_values = []  # This ensures that no stores are selected

    # Apply the search filter to the checklist options
    if not checklist_values or triggered_id == 'select-none-stores':
        checklist_values = []

    return store_options, store_options, checklist_values, is_modal_open

# Callback to render content based on selected tab
@app.callback(Output('tabs-content-example', 'children'),
              Input('tabs-example', 'value'))
def render_content(tab):
    if tab == 'tab-summary':
        return html.Div([
            dbc.Row([
                dbc.Col(dcc.DatePickerRange(
                    id='date-picker-1',
                    start_date_placeholder_text="Start Date",
                    end_date_placeholder_text="End Date",
                    display_format='YYYY-MM-DD'
                ), width=4),
                dbc.Col(dcc.DatePickerRange(
                    id='date-picker-2',
                    start_date_placeholder_text="Start Date",
                    end_date_placeholder_text="End Date",
                    display_format='YYYY-MM-DD'
                ), width=4),
                dbc.Col([
                    dbc.Button("Select Stores", id="open-store-modal", color="primary"),
                    dcc.Dropdown(
                        id='store-dropdown',
                        placeholder="Selected Stores",
                        multi=True,
                        value=[],
                        style={'display': 'none'}
                    ),
                ], width=4),
            ], className="mb-4 custom-row"),
            html.H2("Data Summary", className="text-center mb-4", style={"color": "#333"}),
            dbc.Row(id='data-summary-cards', className="mb-4"),
            html.H2("Time Series Analysis", className="text-center mt-4 mb-4", style={"color": "#333"}),
            dbc.Row([
                dbc.Col(dcc.Dropdown(
                    id='metric-dropdown',
                    options=[
                        {'label': 'Orders', 'value': 'orders'},
                        {'label': 'Units', 'value': 'units'},
                        {'label': 'Fulfilled', 'value': 'fulfilled'},
                        {'label': 'UPH (Units Per Hour)', 'value': 'uph'},
                        {'label': 'Found %', 'value': 'found'},
                        {'label': 'Lates %', 'value': 'lates'},
                        {'label': 'Inf % (Items Not Found)', 'value': 'inf'},
                        {'label': 'Cancelled Orders', 'value': 'cancelled'}
                    ],
                    value=['orders'],  # Default selected metric
                    multi=True,
                    className="custom-dropdown"
                ), width=8),
                dbc.Col(dcc.Dropdown(
                    id='chart-type',
                    options=[
                        {'label': 'Line Chart', 'value': 'Line'},
                        {'label': 'Bar Chart', 'value': 'Bar'},
                        {'label': 'Area Chart', 'value': 'Area'},
                        {'label': 'Pie Chart', 'value': 'Pie'},
                        {'label': 'Heatmap', 'value': 'Heatmap'},
                        {'label': 'Scatter Plot', 'value': 'Scatter'},
                        {'label': 'Bubble Chart', 'value': 'Bubble'}
                    ],
                    value='Line',  # Default chart type
                    placeholder="Select chart type",
                    className="custom-dropdown"
                ), width=4),
                dbc.Col(dcc.Input(
                    id='line-width',
                    type='number',
                    placeholder='Line Width',
                    value=2,  # Default line width
                    min=1,
                    max=10,
                    className="custom-input"
                ), width=4),
            ], className="mb-4 custom-row"),
            dbc.Row([
                dbc.Col(dcc.Graph(id='metric-graph'), width=12, className="custom-graph"),
            ])
        ])
    elif tab == 'tab-table':
        df = fetch_data_cached()
        return html.Div([
            html.H2("Logs Table", className="text-center mb-4", style={"color": "#333"}),
            dash_table.DataTable(
                id='logs-data-table',
                columns=[{"name": i, "id": i} for i in df.columns],
                data=df.to_dict('records'),
                style_cell={
                    'textAlign': 'center',
                    'font-family': 'Roboto',
                    'color': '#333'
                },
                style_header={
                    'backgroundColor': '#f8f9fa',
                    'fontWeight': 'bold',
                    'font-size': '16px',
                    'border': '1px solid #dee2e6'
                },
                style_table={'overflowX': 'auto'},
                style_as_list_view=True
            )
        ], className="custom-table-container")

# Callback to Generate the Metric Graph Based on Selected Metrics and Stores
@app.callback(
    Output('metric-graph', 'figure'),
    Input('metric-dropdown', 'value'),
    Input('chart-type', 'value'),  # New dropdown for selecting chart type
    Input('line-width', 'value'),  # Line thickness customization
    Input('date-picker-1', 'start_date'),
    Input('date-picker-1', 'end_date'),
    Input('date-picker-2', 'start_date'),
    Input('date-picker-2', 'end_date'),
    Input('store-checklist', 'value'),  # Correctly use the selected stores here
    prevent_initial_call=True
)
def update_metric_graph(selected_metrics, chart_type, line_width, start_date_1, end_date_1, start_date_2, end_date_2, selected_stores):
    df = fetch_data_cached()

    # Validate and set default values for date ranges
    if not start_date_1 or not end_date_1:
        start_date_1 = df['timestamp'].min()
        end_date_1 = df['timestamp'].max()

    if not start_date_2 or not end_date_2:
        start_date_2 = start_date_1
        end_date_2 = end_date_1

    # Ensure the date values are in datetime format
    start_date_1 = pd.to_datetime(start_date_1)
    end_date_1 = pd.to_datetime(end_date_1)
    start_date_2 = pd.to_datetime(start_date_2)
    end_date_2 = pd.to_datetime(end_date_2)

    # Filter by date range
    df1 = df[(df['timestamp'] >= start_date_1) & (df['timestamp'] <= end_date_1)]
    df2 = df[(df['timestamp'] >= start_date_2) & (df['timestamp'] <= end_date_2)]

    # Combine the two dataframes for comparison
    combined_df = pd.concat([df1, df2])

    # Filter by selected stores
    if selected_stores and "All Stores" not in selected_stores:
        combined_df = combined_df[combined_df['store'].isin(selected_stores)]

    if combined_df.empty:
        return go.Figure()  # Return an empty figure if there is no data to display

    # Check if more than 10 stores are selected
    if len(selected_stores) > 10:
        # Aggregate data across stores by taking mean for percentages and sum for counts
        combined_df = combined_df.groupby('date').agg({
            'orders': 'sum',
            'units': 'sum',
            'fulfilled': 'sum',
            'uph': 'mean',
            'found': 'mean',
            'lates': 'mean',
            'inf': 'mean',
            'cancelled': 'sum'
        }).reset_index()

    # Aggregate data by day and store
    processed_df = pd.DataFrame()

    for metric in selected_metrics:
        if metric in ['orders', 'units', 'fulfilled', 'cancelled']:
            agg_df = combined_df.groupby(['date']).agg({metric: 'sum'}).reset_index()
        else:
            agg_df = combined_df.groupby(['date']).agg({metric: 'mean'}).reset_index()
        agg_df = agg_df.rename(columns={metric: 'value'})
        agg_df['metric'] = metric
        processed_df = pd.concat([processed_df, agg_df])

    if processed_df.empty:
        return go.Figure()

    # Select the appropriate chart type
    if chart_type == "Line":
        fig = px.line(processed_df, x='date', y='value', color='metric', template="plotly_white")
        fig.update_traces(line=dict(width=line_width))
    elif chart_type == "Bar":
        fig = px.bar(processed_df, x='date', y='value', color='metric', barmode='group', template="plotly_white")
    elif chart_type == "Area":
        fig = px.area(processed_df, x='date', y='value', color='metric', template="plotly_white")
    elif chart_type == "Pie":
        fig = px.pie(processed_df, values='value', names='metric', template="plotly_white")
    elif chart_type == "Heatmap":
        fig = px.density_heatmap(processed_df, x='date', y='metric', z='value', template="plotly_white")
    elif chart_type == "Scatter":
        fig = px.scatter(processed_df, x='date', y='value', color='metric', template="plotly_white")
    elif chart_type == "Bubble":
        fig = px.scatter(processed_df, x='date', y='value', size='value', color='metric', template="plotly_white")

    fig.update_layout(
        hovermode='x unified',
        margin=dict(l=40, r=0, t=40, b=30),
        paper_bgcolor='#fff',
        plot_bgcolor='#fff',
        font=dict(color='#333')
    )

    # Automatically annotate significant changes
    annotations = []
    some_threshold = 100  # Example threshold
    for i, row in processed_df.iterrows():
        if row['value'] > some_threshold:  # Define a threshold for significance
            annotations.append(dict(
                x=row['date'],
                y=row['value'],
                text=f"Spike on {row['date']}",
                showarrow=True,
                arrowhead=2,
                font=dict(color='blue'),
                ax=-20, ay=-40
            ))
    fig.update_layout(annotations=annotations)

    return fig

# Run the app
if __name__ == '__main__':
    app.run_server(debug=True)
