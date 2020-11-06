#!/usr/bin/python3
# vim: set ts=4 expandtab :

import sys
import subprocess

# find_name
# params:
#   f ... file
# return:
#   name
def find_name(f):
    for line in f:
        if line.startswith('  name:'):
            name = line.split(': ')[1].strip()
            return name

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Error: No filename provided.')
        sys.exit(1)
    with open(sys.argv[1], encoding='utf-8', newline='') as log_file:
        for line in log_file:
            if line.startswith('kind:'):
                kind = line.split(': ')[1].strip()
                name = find_name(log_file)
                print('kind=%s name=%s' % (kind, name))
                cmd = 'kubectl delete -n onap %s %s' % (kind, name)
                print('invoking: %s' % cmd)
                comp_proc = subprocess.run(cmd, shell=True)
                print('result; %d' % comp_proc.returncode)
