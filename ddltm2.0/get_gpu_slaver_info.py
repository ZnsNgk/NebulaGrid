import os, json

def get_slaver_info(filename):
    with open(filename) as f:
        gpu_info = json.load(f)
    return gpu_info

def get_psw():
    with open("psw") as f:
        psw = f.readline()
    return psw
