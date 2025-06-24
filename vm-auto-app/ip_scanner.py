import subprocess, ipaddress

def get_free_ip(vlan):
    subnet = {
        'vlan10': '172.16.10.0/24',
        'vlan20': '172.16.20.0/24',
    }.get(vlan)

    if not subnet:
        return None

    net = ipaddress.ip_network(subnet)
    for ip in net.hosts():
        if subprocess.call(['ping', '-c', '1', '-W', '1', str(ip)], stdout=subprocess.DEVNULL) != 0:
            return str(ip)
    return None