from flask import Flask, request, render_template, jsonify
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