import sys
import shlex
import subprocess
import time
import concurrent.futures

verbose = False

def show_usage():
    print("download docker images in parallel.")
    print()
    print("usage: %s <image list file>" % (sys.argv[0]))

def pull_image(image_name):
    print("pulling " + image_name)
    cmd_line = 'docker pull ' + image_name
    cmd_args = shlex.split(cmd_line)
    print("invoking " + cmd_line + " ...")
    p = subprocess.Popen(cmd_args, stdout=subprocess.DEVNULL)
    p.wait()
    print("finish " + image_name + ", returncode=" + p.returncode)
    return p.returncode

if verbose:
    print(sys.argv)

if len(sys.argv) == 1:
    show_usage()
    sys.exit(0)

executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)
future_list = []

with open(sys.argv[1]) as f:
    for image_name_raw in f:
        image_name = image_name_raw.strip()
        if image_name.startswith('#'):
            continue
        print("submit " + image_name)
        future = executor.submit(pull_image, image_name)
        future_list.append(future)

concurrent.futures.wait(future_list)
