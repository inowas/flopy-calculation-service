from datetime import datetime

from FlopyAdapter.Read import ReadBudget, ReadHead, ReadConcentration, ReadDrawdown
from flask import abort, Flask, request, redirect, render_template, Response, send_file
from flask_cors import CORS, cross_origin
import pandas as pd
import os
from pathlib import Path

import prometheus_client
from prometheus_flask_exporter import PrometheusMetrics
import sqlite3 as sql
import urllib.request
import json
import jsonschema
import shutil
import uuid
import zipfile
import io

DB_LOCATION = '/db/modflow.db'
MODFLOW_FOLDER = '/modflow'
UPLOAD_FOLDER = './uploads'
SCHEMA_SERVER_URL = 'https://schema.inowas.com'

app = Flask(__name__)
CORS(app)
metrics = PrometheusMetrics(app)

g_0 = prometheus_client.Gauge('number_of_calculated_models_0', 'Calculations in queue')
g_100 = prometheus_client.Gauge('number_of_calculated_models_100', 'Calculations in progress')
g_200 = prometheus_client.Gauge('number_of_calculated_models_200', 'Calculations finished with success')
g_400 = prometheus_client.Gauge('number_of_calculated_models_400', 'Calculations finished with error')


def db_init():
    conn = db_connect()

    sql_command = """
        CREATE TABLE IF NOT EXISTS calculations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            calculation_id STRING, 
            state INTEGER, 
            message TEXT, 
            created_at DATE, 
            updated_at DATE
        )
    """
    conn.execute(sql_command)


def db_connect():
    return sql.connect(DB_LOCATION)


# noinspection SqlResolve
def get_calculation_by_id(calculation_id):
    conn = db_connect()
    conn.row_factory = sql.Row
    cursor = conn.cursor()

    cursor.execute(
        'SELECT calculation_id, state, message FROM calculations WHERE calculation_id = ?', (calculation_id,)
    )
    return cursor.fetchone()


def get_number_of_calculations(state=200):
    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute(
        'SELECT Count() FROM calculations WHERE state = ?', (state,)
    )

    return cursor.fetchone()[0]


def get_calculation_details_json(calculation_id, data, path):
    calculation = get_calculation_by_id(calculation_id)

    mfLogfile = os.path.join(path, 'modflow.log')
    if os.path.isfile(mfLogfile):
        calculation['message'] = Path(mfLogfile).read_text()

    heads = ReadHead(path)
    budget_times = ReadBudget(path).read_times()
    concentration_times = ReadConcentration(path).read_times()
    drawdown_times = ReadDrawdown(path).read_times()

    total_times = [float(totim) for totim in heads.read_times()]

    times = {
        'start_date_time': data['dis']['start_datetime'],
        'time_unit': data['dis']['itmuni'],
        'total_times': total_times
    }

    layer_values = []
    number_of_layers = data['dis']['nlay']

    lv = ['head']
    if len(budget_times) > 0:
        lv.append('budget')

    if len(concentration_times) > 0:
        lv.append('concentration')

    if len(drawdown_times) > 0:
        lv.append('drawdown')

    for i in range(0, number_of_layers):
        layer_values.append(lv)

    target_directory = os.path.join(app.config['MODFLOW_FOLDER'], calculation_id)

    return json.dumps({
        'calculation_id': calculation_id,
        'state': calculation['state'],
        'message': calculation['message'],
        'files': os.listdir(target_directory),
        'times': times,
        'layer_values': layer_values
    })


def valid_json_file(file):
    with open(file) as filedata:
        try:
            json.loads(filedata.read())
        except ValueError:
            return False
        return True


def read_json(file):
    with open(file) as filedata:
        data = json.loads(filedata.read())
    return data


def is_valid(content):
    try:
        data = content.get('data')
        mf = data.get('mf')
        mt = data.get('mt')
    except AttributeError:
        return False

    try:
        mf_schema_data = urllib.request.urlopen('{}/modflow/packages/mfPackages.json'.format(SCHEMA_SERVER_URL))
        mf_schema = json.loads(mf_schema_data.read())
        jsonschema.validate(instance=mf, schema=mf_schema)
    except jsonschema.exceptions.ValidationError:
        return False

    if mt:
        try:
            mt_schema_data = urllib.request.urlopen('{}/modflow/packages/mtPackages.json'.format(SCHEMA_SERVER_URL))
            mt_schema = json.loads(mt_schema_data.read())
            jsonschema.validate(instance=mt, schema=mt_schema)
        except jsonschema.exceptions.ValidationError:
            return False

    return True


# noinspection SqlResolve
def insert_new_calculation(calculation_id):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute('INSERT INTO calculations (calculation_id, state, created_at, updated_at) VALUES ( ?, ?, ?, ?)',
                    (calculation_id, 0, datetime.now(), datetime.now()))


def is_binary(filename):
    """
    Return true if the given filename appears to be binary.
    File is considered to be binary if it contains a NULL byte.
    FIXME: This approach incorrectly reports UTF-16 as binary.
    """
    with open(filename, 'rb') as f:
        for block in f:
            if b'\0' in block:
                return True
    return False


@app.route('/', methods=['GET', 'POST'])
@cross_origin()
def upload_file():
    if request.method == 'POST':

        if 'multipart/form-data' in request.content_type:
            # check if the post request has the file part
            if 'file' not in request.files:
                abort(415, 'No file uploaded')

            uploaded_file = request.files['file']
            if uploaded_file.filename == '':
                abort(415, 'No selected file')

            temp_filename = str(uuid.uuid4()) + '.json'
            temp_file = os.path.join(app.config['UPLOAD_FOLDER'], temp_filename)
            uploaded_file.save(temp_file)

            content = read_json(temp_file)

            if not is_valid(content):
                os.remove(temp_file)
                abort(422, 'This JSON file does not match with the MODFLOW JSON Schema')

            calculation_id = content.get("calculation_id")
            target_directory = os.path.join(app.config['MODFLOW_FOLDER'], calculation_id)
            modflow_file = os.path.join(target_directory, 'configuration.json')

            if os.path.exists(modflow_file):
                abort(422, 'Model with calculationId: {} already exits.'.format(calculation_id))

            os.makedirs(target_directory)
            with open(modflow_file, 'w') as outfile:
                json.dump(content, outfile)

            insert_new_calculation(calculation_id)

            return redirect('/' + calculation_id)

        if 'application/json' in request.content_type:
            content = request.get_json(force=True)

            if not is_valid(content):
                abort(422, 'Content is not valid.')

            calculation_id = content.get('calculation_id')
            target_directory = os.path.join(app.config['MODFLOW_FOLDER'], calculation_id)
            modflow_file = os.path.join(target_directory, 'configuration.json')

            if os.path.exists(modflow_file):
                print('Path exists.')
                if not os.path.exists(os.path.join(target_directory, 'state.log')):
                    print('State-log not existing, remove folder.')
                    shutil.rmtree(target_directory, ignore_errors=True)

                if Path(os.path.join(target_directory, 'state.log')).read_text() != '200':
                    print('State-log existing, but not 200. Remove folder.')
                    shutil.rmtree(target_directory, ignore_errors=True)

            if not os.path.exists(modflow_file):
                print('Create folder.')
                os.makedirs(target_directory)
                with open(modflow_file, 'w') as outfile:
                    json.dump(content, outfile)

                insert_new_calculation(calculation_id)

            return json.dumps({
                'status': 200,
                'calculation_id': calculation_id,
                'link': '/' + calculation_id
            })

    if request.method == 'GET':
        return render_template('upload.html')


@app.route('/<calculation_id>', methods=['GET'])
@cross_origin()
def calculation_details(calculation_id):
    modflow_file = os.path.join(app.config['MODFLOW_FOLDER'], calculation_id, 'configuration.json')
    if not os.path.exists(modflow_file):
        abort(404, 'Calculation with id: {} not found.'.format(calculation_id))

    data = read_json(modflow_file).get('data').get('mf')
    path = os.path.join(app.config['MODFLOW_FOLDER'], calculation_id)

    if request.content_type and 'application/json' in request.content_type:
        return get_calculation_details_json(calculation_id, data, path)

    return render_template('details.html', id=str(calculation_id), data=data, path=path)


@app.route('/<calculation_id>/files/<file_name>', methods=['GET'])
@cross_origin()
def get_file(calculation_id, file_name):
    target_file = os.path.join(app.config['MODFLOW_FOLDER'], calculation_id, file_name)

    if not os.path.exists(target_file):
        abort(404, {'message': 'File with name {} not found.'.format(file_name)})

    if is_binary(target_file):
        return json.dumps({
            'name': file_name,
            'content': 'This file is a binary file and cannot be shown as text'
        })

    with open(target_file) as f:
        file_content = f.read()
        return json.dumps({
            'name': file_name,
            'content': file_content
        })


@app.route('/<calculation_id>/results/types/<t>/layers/<layer>/totims/<totim>', methods=['GET'])
@cross_origin()
def get_results_head_drawdown(calculation_id, t, layer, totim):
    target_folder = os.path.join(app.config['MODFLOW_FOLDER'], calculation_id)
    modflow_file = os.path.join(target_folder, 'configuration.json')

    if not os.path.exists(modflow_file):
        abort(404, 'Calculation with id: {} not found.'.format(calculation_id))

    permitted_types = ['head', 'drawdown']

    totim = float(totim)
    layer = int(layer)

    if t not in permitted_types:
        abort(404,
              'Type: {} not in the list of permitted types. \
              Permitted types are: {}.'.format(t, ", ".join(permitted_types))
              )

    if t == 'head':
        heads = ReadHead(target_folder)
        times = heads.read_times()

        if totim not in times:
            abort(404, 'Totim: {} not available. Available totims are: {}'.format(totim, ", ".join(map(str, times))))

        nlay = heads.read_number_of_layers()
        if layer >= nlay:
            abort(404, 'Layer must be less then the overall number of layers ({}).'.format(nlay))

        return json.dumps(heads.read_layer(totim, layer))

    if t == 'drawdown':
        drawdown = ReadDrawdown(target_folder)
        times = drawdown.read_times()
        if totim not in times:
            abort(404, 'Totim: {} not available. Available totims are: {}'.format(totim, ", ".join(map(str, times))))

        nlay = drawdown.read_number_of_layers()
        if layer >= nlay:
            abort(404, 'Layer must be less then the overall number of layers ({}).'.format(nlay))

        return json.dumps(drawdown.read_layer(totim, layer))


@app.route('/<calculation_id>/timeseries/types/<t>/layers/<layer>/rows/<row>/columns/<column>', methods=['GET'])
@cross_origin()
def get_results_time_series(calculation_id, t, layer, row, column):
    target_folder = os.path.join(app.config['MODFLOW_FOLDER'], calculation_id)
    modflow_file = os.path.join(target_folder, 'configuration.json')

    if not os.path.exists(modflow_file):
        abort(404, 'Calculation with id: {} not found.'.format(calculation_id))

    permitted_types = ['head', 'drawdown']

    layer = int(layer)
    row = int(row)
    col = int(column)

    if t not in permitted_types:
        abort(404,
              'Type: {} not in the list of permitted types. \
              Permitted types are: {}.'.format(t, ", ".join(permitted_types))
              )

    if t == 'head':
        heads = ReadHead(target_folder)
        return json.dumps(heads.read_ts(layer, row, col))

    if t == 'drawdown':
        drawdown = ReadDrawdown(target_folder)
        return json.dumps(drawdown.read_ts(layer, row, col))


@app.route('/<calculation_id>/results/types/budget/totims/<totim>', methods=['GET'])
@cross_origin()
def get_results_budget_by_totim(calculation_id, totim):
    target_folder = os.path.join(app.config['MODFLOW_FOLDER'], calculation_id)
    modflow_file = os.path.join(target_folder, 'configuration.json')

    if not os.path.exists(modflow_file):
        abort(404, 'Calculation with id: {} not found.'.format(calculation_id))

    totim = float(totim)

    budget = ReadBudget(target_folder)
    times = budget.read_times()
    if totim not in times:
        abort(404, 'Totim: {} not available. Available totims are: {}'.format(totim, ", ".join(map(str, times))))

    return json.dumps({
        'cumulative': budget.read_budget_by_totim(totim, incremental=False),
        'incremental': budget.read_budget_by_totim(totim, incremental=True)
    })


@app.route('/<calculation_id>/results/types/budget/idx/<idx>', methods=['GET'])
@cross_origin()
def get_results_budget_by_idx(calculation_id, idx):
    target_folder = os.path.join(app.config['MODFLOW_FOLDER'], calculation_id)
    modflow_file = os.path.join(target_folder, 'configuration.json')

    if not os.path.exists(modflow_file):
        abort(404, 'Calculation with id: {} not found.'.format(calculation_id))

    idx = int(idx)

    budget = ReadBudget(target_folder)
    times = budget.read_times()
    if idx >= len(times):
        abort(404,
              'TotimKey: {} not available. Available keys are in between: {} and {}'.format(idx, 0, len(times) - 1))

    return json.dumps({
        'cumulative': budget.read_budget_by_idx(idx=idx, incremental=False),
        'incremental': budget.read_budget_by_idx(idx=idx, incremental=True)
    })


@app.route(
    '/<calculation_id>/results/types/concentration/substance/<substance>/layers/<layer>/totims/<totim>',
    methods=['GET'])
@cross_origin()
def get_results_concentration(calculation_id, substance, layer, totim):
    target_folder = os.path.join(app.config['MODFLOW_FOLDER'], calculation_id)
    modflow_file = os.path.join(target_folder, 'configuration.json')

    if not os.path.exists(modflow_file):
        abort(404, 'Calculation with id: {} not found.'.format(calculation_id))

    layer = int(layer)
    substance = int(substance)
    totim = float(totim)

    concentrations = ReadConcentration(target_folder)

    nsub = concentrations.read_number_of_substances()
    if substance >= nsub:
        abort(404, 'Substance: {} not available. Number of substances: {}.'.format(substance, nsub))

    times = concentrations.read_times()
    if totim not in times:
        abort(404, 'Totim: {} not available. Available totims are: {}'.format(totim, ", ".join(map(str, times))))

    nlay = concentrations.read_number_of_layers()
    if layer >= nlay:
        abort(404, 'Layer must be less then the overall number of layers ({}).'.format(nlay))

    return json.dumps(concentrations.read_layer(substance, totim, layer))


@app.route('/<calculation_id>/results/types/observations', methods=['GET'])
@cross_origin()
def get_results_observations(calculation_id):
    target_folder = os.path.join(app.config['MODFLOW_FOLDER'], calculation_id)
    hob_out_file = os.path.join(target_folder, 'mf.hob.out')

    if not os.path.exists(hob_out_file):
        abort(404, 'Head observations from calculation with id: {} not found.'.format(calculation_id))

    try:
        df = pd.read_csv(hob_out_file, delim_whitespace=True, header=0, names=['simulated', 'observed', 'name'])
        return df.to_json(orient='records')
    except:
        abort(500, 'Error converting head observation output file.')


@app.route('/<calculation_id>/download', methods=['GET'])
@cross_origin()
def get_download_model(calculation_id):
    os.chdir(os.path.join(app.config['MODFLOW_FOLDER'], calculation_id))
    data = io.BytesIO()
    with zipfile.ZipFile(data, mode='w') as z:
        for root, dirs, files in os.walk("."):
            for filename in files:
                if not filename.endswith('.json'):
                    z.write(filename)

    data.seek(0)
    return send_file(
        data,
        mimetype='application/zip',
        as_attachment=True,
        attachment_filename='model-calculation-{}.zip'.format(calculation_id)
    )


# noinspection SqlResolve
@app.route('/list')
def list():
    con = db_connect()
    con.row_factory = sql.Row

    cur = con.cursor()
    cur.execute('select * from calculations')

    rows = cur.fetchall()
    return render_template("list.html", rows=rows)


@app.route('/metrics')
def metrics():
    g_0.set(get_number_of_calculations(0))
    g_100.set(get_number_of_calculations(100))
    g_200.set(get_number_of_calculations(200))
    g_400.set(get_number_of_calculations(400))
    CONTENT_TYPE_LATEST = str('text/plain; version=0.0.4; charset=utf-8')
    return Response(prometheus_client.generate_latest(), mimetype=CONTENT_TYPE_LATEST)


if __name__ == '__main__':
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)

    app.secret_key = '2349978342978342907889709154089438989043049835890'
    app.config['SESSION_TYPE'] = 'filesystem'
    app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
    app.config['MODFLOW_FOLDER'] = MODFLOW_FOLDER

    db_init()
    app.run(debug=True, host='0.0.0.0')
