from flask import Flask, request, render_template, jsonify
import os
import ip_scanner
import vmware_lib

app = Flask(__name__)

@app.route('/')
def form():
    return render_template('form.html')

@app.route('/create-vm', methods=['POST'])
def create_vm():
    vm_name = request.form['vm_name']
    vlan = request.form['vlan']
    datastore = request.form['datastore']

    ip = ip_scanner.get_free_ip(vlan)
    if not ip:
        return jsonify({'error': 'No free IPs available'}), 400

    result = vmware_lib.deploy_vm(vm_name, vlan, datastore, ip)
    return jsonify({'message': f'VM {vm_name} deployed with IP {ip}'} if result else {'error': 'VM creation failed'})

@app.route('/macos-folders')
def list_macos_folders():
    docs_path = os.path.expanduser('~/Documents')
    folders = []
    if os.path.isdir(docs_path):
        folders = sorted(
            entry for entry in os.listdir(docs_path)
            if os.path.isdir(os.path.join(docs_path, entry))
        )
    return render_template('macos_folders.html', folders=folders, path=docs_path)

@app.route('/api/macos-folders')
def api_macos_folders():
    docs_path = os.path.expanduser('~/Documents')
    folders = []
    if os.path.isdir(docs_path):
        folders = sorted(
            entry for entry in os.listdir(docs_path)
            if os.path.isdir(os.path.join(docs_path, entry))
        )
    return jsonify({'path': docs_path, 'folders': folders})