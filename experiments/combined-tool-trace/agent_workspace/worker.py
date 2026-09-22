import subprocess

p = subprocess.Popen(["sleep", "3"])
print(p.pid)
p.wait()
print("done")
