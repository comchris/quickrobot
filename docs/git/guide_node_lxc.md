











sudo mkdir -p /vmdata/lxc/gpubox/
sudo debootstrap trixie /vmdata/lxc/gpubox/  http://deb.debian.org/debian/
sudo echo "GPUbox" > /vmdata/lxc/gpubox/etc/hostname
sudo mkdir -p /vmdata/lxc/gpubox/root/.ssh
sudo nano /vmdata/lxc/gpubox/root/.ssh/authorized_keys

sudo chroot /vmdata/lxc/gpubox/


sudo virt-install --connect lxc:/// --name GPUbox --ram 8192 --os-variant debian13 \
--filesystem /vmdata/lxc/gpubox/,/ --boot init=/lib/systemd/systemd --network network=dhcptest



## Remote Hosts: (suggestion!)

- needs passwordless sudo for ansible: 
- push a key with ssh-copy-id & use visudo to add

iamauser ALL=(ALL) NOPASSWD: ALL

