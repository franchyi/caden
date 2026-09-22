# Notes

## on cgroup

cgroup slice defaults to caden-experiment.slice

### Disabling the sudo prompt

Creating the cgroup in the script requires sudo
To disable the admin prompt: run this:

```bash
sudo loginctl enable-linger $(whoami)
# Now this should show Linger=yes
loginctl show-user $(whoami) --property=Linger
```