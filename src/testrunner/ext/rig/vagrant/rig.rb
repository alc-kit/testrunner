# testrunner.ext.rig's Vagrant layer: the DEFAULT Vagrantfile for a vagrant + libvirt test rig,
# owned by testrunner, which a rig loads and extends with hooks — or does not load at all.
#
# A rig's own Vagrantfile is the seam (WP testrunner §6c.6, T20). Vagrant evaluates exactly
# that file, so a rig picks one of:
#
#   (a) the default, as is:
#         require_relative "testrunner/src/testrunner/ext/rig/vagrant/rig"
#         RigVagrant.configure(root: __dir__, network: "my-rig-net")
#   (b) the default with hooks for what this rig does differently:
#         RigVagrant.configure(root: __dir__) do |rig|
#           rig.networks { |node, name, spec| ... }        # replaces the default network
#           rig.machines { |nodes| ... }                   # filter / change the machine table
#           rig.autostart { |name, spec| ... }             # default: not for catalogue `late:`
#           rig.domain { |lv, name, spec| ... }            # after the default libvirt settings
#           rig.provision(:after_names) { |node, name, spec| ... }   # positions: POSITIONS
#         end
#   (c) a Vagrantfile of its own that never requires this file; or
#   (d) no Vagrantfile and no vagrant at all (ext.rig's machine-provider seam).
#
# What it builds, from the node catalogue (`bin/rig-nodes json`, the ONE reader) and the
# rig's environment (<PREFIX>_*, the prefix in testrunner.yml `ext.rig.prefix`):
#   * every catalogue machine (order kept: `vagrant up --no-parallel` follows it); sizes
#     from the catalogue, each `overrides: {field: key}` replaced by <PREFIX>_<KEY> when > 0;
#   * libvirt on the SYSTEM daemon, host-passthrough + nested; on `kind: pve` machines the
#     NIC model (<PREFIX>_NIC_MODEL; q35 for an emulated Intel NIC), virtio ctrl_vlan
#     (<PREFIX>_VIRTIO_CTRL_VLAN) and an emulated IPMI BMC; blank virtio data disks;
#   * on `kind: pve` machines, one rebooting provisioner for predictable NIC names
#     (<PREFIX>_NIC_NAMES) and the serial console (<PREFIX>_CONSOLE);
#   * the apt cache (<CACHE_PREFIX>_APT_CACHE / _APT_CACHE_HTTP / _APT_SEED): a squid box
#     (the catalogue's machine marked `optional: apt_cache`, configured from
#     provision/squid.conf), and every other box pointed at it;
#   * root login with the harness key (secrets/harness_key.pub) and a known root password.
#
# Nothing rig- or customer-named lives here: network names, addresses and MACs come from the
# catalogue or the rig's hooks; the box, the root password and the cache port are parameters.
# The Vagrantfile is evaluated by EVERY vagrant command, a teardown too: what a teardown does
# not need warns and carries on (a missing squid.conf disables the cache), never raises.

require "json"
require "yaml"

module RigVagrant
  NIC_MODELS = %w[virtio e1000e igb].freeze
  POSITIONS = %i[after_names after_key].freeze

  def self.configure(**kw)
    rig = Rig.new(**kw)
    yield rig if block_given?
    rig.apply
    rig
  end

  class Rig
    attr_reader :root, :name, :prefix, :cache_prefix, :label

    def initialize(root:, name: nil, prefix: nil, cache_prefix: nil, label: nil, network: nil,
                   box: "debian/trixie64", root_password: "vagrantlab", cache_port: 3128,
                   squid_conf: "provision/squid.conf")
      @root = root
      ext = ext_rig_settings
      @name = name || ext["name"] || File.basename(root)
      @prefix = prefix || ext["prefix"] || abort("#{@name}: no environment prefix " \
                                                 "(testrunner.yml ext.rig.prefix, or prefix:)")
      @cache_prefix = cache_prefix || @prefix
      @label = label || @name
      @network, @box, @root_password, @cache_port = network, box, root_password, cache_port
      @squid_conf_path = File.join(root, squid_conf)
      @hooks = { networks: nil, machines: nil, autostart: nil, domain: nil }
      @provision = Hash.new { |h, k| h[k] = [] }
    end

    # ── hooks ──
    def networks(&blk)
      @hooks[:networks] = blk
    end

    def machines(&blk)
      @hooks[:machines] = blk
    end

    def autostart(&blk)
      @hooks[:autostart] = blk
    end

    def domain(&blk)
      @hooks[:domain] = blk
    end

    def provision(position, &blk)
      abort "#{@name}: provision position #{position.inspect} is not one of #{POSITIONS.inspect}" \
        unless POSITIONS.include?(position)
      @provision[position] << blk
    end

    # ── the rig's environment ──
    def env(knob, default)
      ENV.fetch("#{@prefix}_#{knob}", default)
    end

    def knob(knob, default:, allowed:)
      v = env(knob, default)
      abort "#{@prefix}_#{knob} must be one of #{allowed.join(', ')} (got '#{v}')" unless allowed.include?(v)
      v
    end

    def nic_model
      @nic_model ||= begin
        v = env("NIC_MODEL", "virtio")
        abort "#{@prefix}_NIC_MODEL must be one of #{NIC_MODELS.join(', ')} (got '#{v}')" unless NIC_MODELS.include?(v)
        v
      end
    end

    def virtio_ctrl_vlan
      @virtio_ctrl_vlan ||= begin
        v = env("VIRTIO_CTRL_VLAN", "on")
        abort "#{@prefix}_VIRTIO_CTRL_VLAN must be 'on' or 'off' (got '#{v}')" unless %w[on off].include?(v)
        abort "#{@prefix}_VIRTIO_CTRL_VLAN=off needs #{@prefix}_NIC_MODEL=virtio (got '#{nic_model}')" \
          if v == "off" && nic_model != "virtio"
        v
      end
    end

    def nic_names
      @nic_names ||= begin
        v = env("NIC_NAMES", "predictable")
        abort "#{@prefix}_NIC_NAMES must be predictable or kernel (got '#{v}')" unless %w[predictable kernel].include?(v)
        v
      end
    end

    def console
      @console ||= begin
        v = env("CONSOLE", "log")
        abort "#{@prefix}_CONSOLE must be log or off (got '#{v}')" unless %w[log off].include?(v)
        v
      end
    end

    # ── the catalogue ──
    def rig_nodes(*args)
      out = IO.popen([File.join(@root, "bin", "rig-nodes"), *args], err: [:child, :out], &:read)
      abort "bin/rig-nodes #{args.join(' ')} failed — the machine table cannot be built:\n#{out}" unless $?.success?
      out
    end

    def catalogue
      @catalogue ||= JSON.parse(rig_nodes("json")).fetch("machines").freeze
    end

    # name => { kind, ip, mem, cpu, disks, size, late, optional, machine }, in catalogue order;
    # each `overrides: {field: key}` replaced by <PREFIX>_<KEY> when that is set (> 0)
    def nodes
      @nodes ||= begin
        table = catalogue.to_h do |m|
          spec = { kind: m["kind"], ip: m["ip"], mem: m["mem"], cpu: m["cpu"], disks: m["disks"], size: m["size"],
                   late: m["late"] ? true : false, optional: m["optional"], machine: m }
          (m["overrides"] || {}).each do |field, key|
            v = env(key.upcase, "0").to_i
            spec[field.to_sym] = v if v > 0
          end
          [m["name"], spec]
        end
        (@hooks[:machines] ? @hooks[:machines].call(table) : table).freeze
      end
    end

    # ── the apt cache ──
    def cache_enabled
      ENV.fetch("#{@cache_prefix}_APT_CACHE", "1") != "0"
    end

    def cache_http
      ENV.fetch("#{@cache_prefix}_APT_CACHE_HTTP", "1") != "0"
    end

    def cache_seed
      ENV.fetch("#{@cache_prefix}_APT_SEED",
                File.join(Dir.home, ".cache", "#{@cache_prefix.downcase}-apt-cache-seed.tar.xz"))
    end

    def cache_seed_found
      [cache_seed, cache_seed.sub(/\.xz\z/, "")].find { |f| File.exist?(f) }
    end

    def cache_machine
      nodes.find { |_, s| s[:optional] == "apt_cache" }
    end

    def cache_ip
      cache_machine && cache_machine[1][:ip]
    end

    # enabled, a cache machine in the catalogue, and its squid.conf present; a missing file
    # warns and disables the cache for this invocation (a teardown must never fail on it)
    def cache_ready
      return @cache_ready unless @cache_ready.nil?
      @cache_ready = cache_enabled && !cache_machine.nil? && File.exist?(@squid_conf_path)
      if cache_enabled && cache_machine && !@cache_ready
        warn "#{@label}: squid.conf not found at #{@squid_conf_path} — apt cache DISABLED for this invocation " \
             "(it is a tracked file of the rig: is the checkout complete?)."
      end
      @cache_ready
    end

    # ── the Vagrantfile ──
    def apply
      rig = self
      pubkey_path = File.join(@root, "secrets", "harness_key.pub")
      Vagrant.configure("2") do |config|
        config.vm.box = @box
        # Root SSH is managed by the harness; vagrant's own key for provisioning only, and
        # never the operator's ssh-agent in either direction (AddKeysToAgent=no alone proved
        # insufficient: IdentityAgent=none stops vagrant's ssh consulting an agent at all).
        config.ssh.insert_key = false
        config.ssh.forward_agent = false
        config.ssh.extra_args = ["-o", "AddKeysToAgent=no", "-o", "IdentityAgent=none"]
        config.vm.synced_folder ".", "/vagrant", disabled: true

        unless File.exist?(pubkey_path)
          raise "Missing #{pubkey_path} — run ./run.sh (or: ssh-keygen -t ed25519 -N '' -f secrets/harness_key) first."
        end
        pubkey = File.read(pubkey_path).strip
        squid_conf = rig.cache_ready ? File.read(@squid_conf_path) : ""

        rig.nodes.each do |name, spec|
          next if spec[:optional] == "apt_cache" && !rig.cache_ready
          config.vm.define name, autostart: rig.autostart_of(name, spec) do |node|
            node.vm.hostname = name
            if @hooks[:networks]
              @hooks[:networks].call(node, name, spec)
            else
              rig.default_network(node, spec)
            end
            node.vm.provider :libvirt do |lv|
              rig.default_domain(lv, spec)
              @hooks[:domain]&.call(lv, name, spec)
            end
            rig.names_and_console(node, spec)
            @provision[:after_names].each { |h| h.call(node, name, spec) }
            rig.cache_client(node, name)
            rig.root_login(node, pubkey)
            @provision[:after_key].each { |h| h.call(node, name, spec) }
            rig.cache_box(node, squid_conf) if spec[:optional] == "apt_cache"
          end
        end
      end
    end

    def autostart_of(name, spec)
      @hooks[:autostart] ? @hooks[:autostart].call(name, spec) : !spec[:late]
    end

    # one isolated harness network, static addresses, NAT — the network `network:` names
    def default_network(node, spec)
      abort "#{@name}: no network name — pass network: to RigVagrant.configure, or a networks hook" unless @network
      node.vm.network :private_network,
        ip: spec[:ip],
        libvirt__network_name: @network,
        libvirt__dhcp_enabled: false,
        libvirt__forward_mode: "nat"
    end

    def default_domain(lv, spec)
      # the SYSTEM daemon: session mode cannot do the bridged NAT network + inter-VM L2
      lv.uri = "qemu:///system"
      lv.qemu_use_session = false
      lv.memory  = spec[:mem]
      lv.cpus    = spec[:cpu]
      lv.cpu_mode = "host-passthrough"   # expose vmx/svm: the machine is a hypervisor
      lv.nested  = true
      if spec[:kind] == "pve"
        lv.nic_model_type = nic_model
        # an emulated Intel NIC needs q35 (QEMU's igb is SR-IOV: on i440fx a second igb's VF
        # slots collide with the first's); vagrant-libvirt 0.11.2 sets EVERY interface's model
        lv.machine_type = "q35" if nic_model != "virtio"
        if virtio_ctrl_vlan == "off"
          lv.qemuargs value: "-global"
          lv.qemuargs value: "virtio-net-pci.ctrl_vlan=off"
        end
        # a BMC, as real servers have (openipmi fails every boot without one); QEMU emulates
        # it, the kernel finds it through SMBIOS (/dev/ipmi0)
        lv.qemuargs value: "-device"
        lv.qemuargs value: "ipmi-bmc-sim,id=bmc0"
        lv.qemuargs value: "-device"
        lv.qemuargs value: "isa-ipmi-kcs,bmc=bmc0"
      end
      (1..spec[:disks]).each do
        lv.storage :file, size: "#{spec[:size]}G", type: "qcow2", bus: "virtio"
      end
    end

    # FIRST provisioner on `kind: pve` machines; one reboot covers both. NIC NAMES: ask udev
    # which name each NIC WILL get (onboard, else slot, else path), rename the box's ifupdown
    # stanzas to it, drop net.ifnames=0 / biosdevname=0. SERIAL CONSOLE: GRUB, kernel and
    # initramfs on ttyS0 as well (the harness captures ttyS0 on the host).
    def names_and_console(node, spec)
      return unless spec[:kind] == "pve" && (nic_names == "predictable" || console == "log")
      node.vm.provision "shell", privileged: true, reboot: true, inline: <<-SHELL
          set -eu
          names=#{nic_names == "predictable" ? 1 : 0}
          console=#{console == "log" ? 1 : 0}
          if [ "$names" = 1 ] && grep -qw 'net.ifnames=0' /proc/cmdline; then
            for d in /sys/class/net/*; do
              [ -e "$d/device" ] || continue
              old="$(basename "$d")"; new=""
              for k in ONBOARD SLOT PATH; do
                new="$(udevadm test-builtin net_id "$d" 2>/dev/null | sed -n "s/^ID_NET_NAME_$k=//p" | head -1)"
                [ -n "$new" ] && break
              done
              [ -n "$new" ] || { echo "FATAL: udev predicts no name for $old" >&2; exit 1; }
              echo "$old -> $new"
              sed -i -E "s/\\\\b$old\\\\b/$new/g" /etc/network/interfaces
              [ -d /etc/network/interfaces.d ] && find /etc/network/interfaces.d -type f -exec sed -i -E "s/\\\\b$old\\\\b/$new/g" {} +
            done
            sed -i -E 's/[[:space:]]*(net\\.ifnames=0|biosdevname=0)//g' /etc/default/grub
          fi
          if [ "$console" = 1 ] && ! grep -q 'console=ttyS0' /etc/default/grub; then
            sed -i -E 's/^(GRUB_CMDLINE_LINUX="[^"]*)"/\\1 console=tty0 console=ttyS0,115200n8"/' /etc/default/grub
            if grep -q '^GRUB_TERMINAL=' /etc/default/grub; then
              sed -i -E 's/^GRUB_TERMINAL=.*/GRUB_TERMINAL="console serial"/' /etc/default/grub
            else
              echo 'GRUB_TERMINAL="console serial"' >> /etc/default/grub
            fi
            grep -q '^GRUB_SERIAL_COMMAND=' /etc/default/grub \\
              || echo 'GRUB_SERIAL_COMMAND="serial --unit=0 --speed=115200"' >> /etc/default/grub
          fi
          grep -nE '^GRUB_(CMDLINE|TERMINAL|SERIAL)' /etc/default/grub
          update-grub 2>&1 | tail -1
          grep -nE 'iface|allow-hotplug|auto ' /etc/network/interfaces
        SHELL
    end

    # every box but the cache itself, BEFORE anything runs apt: http(s) through the cache
    # (an https CONNECT tunnel is not cacheable, but proxied it is measurable); with
    # _APT_CACHE_HTTP the Debian sources move to http so squid can cache them too
    def cache_client(node, name)
      return unless cache_ready && name != cache_machine[0]
      node.vm.provision "shell", privileged: true, inline: <<-SHELL
          set -eu
          cat > /etc/apt/apt.conf.d/01gate3-cache <<'EOF'
// Harness apt cache. Written by the Vagrantfile at box creation.
Acquire::http::Proxy "http://#{cache_ip}:#{@cache_port}";
Acquire::https::Proxy "http://#{cache_ip}:#{@cache_port}";
EOF
          chmod 0644 /etc/apt/apt.conf.d/01gate3-cache
#{cache_http ? %{
          for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
            [ -f "$f" ] || continue
            sed -i 's|https://deb.debian.org|http://deb.debian.org|g; \
                    s|https://security.debian.org|http://security.debian.org|g' "$f"
          done
} : ""}
        SHELL
    end

    # root login for Ansible, and a known root password (the bootstrap identity)
    def root_login(node, pubkey)
      node.vm.provision "shell", privileged: true, inline: <<-SHELL
        set -eu
        install -d -m 0700 /root/.ssh
        grep -qxF '#{pubkey}' /root/.ssh/authorized_keys 2>/dev/null || echo '#{pubkey}' >> /root/.ssh/authorized_keys
        chmod 0600 /root/.ssh/authorized_keys
        sed -ri 's/^#?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
        systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
        echo 'root:#{@root_password}' | chpasswd
        command -v python3 >/dev/null 2>&1 || { apt-get update && apt-get install -y python3; }
      SHELL
    end

    # the cache box: squid from squid.conf (read here, written verbatim — the ansible stage
    # re-applies the same bytes to a running rig), a seed restored when one exists (to
    # /var/tmp: /tmp is a small tmpfs), and not declared up until squid has SERVED a request
    def cache_box(node, squid_conf)
      if cache_seed_found
        node.vm.provision "file", source: cache_seed_found,
                                  destination: "/var/tmp/gate3-squid-seed"
      end
      node.vm.provision "shell", privileged: true, inline: <<-SHELL
          set -eu
          export DEBIAN_FRONTEND=noninteractive
          apt-get update
          apt-get install -y squid curl xz-utils
          cat > /etc/squid/squid.conf <<'GATE3_SQUID_EOF'
#{squid_conf}
GATE3_SQUID_EOF
          systemctl stop squid || true
          if [ -f /var/tmp/gate3-squid-seed ]; then
            # Format from the MAGIC BYTES, not the filename: the seed path is
            # configurable, so a compressed archive can arrive under any name.
            magic=$(head -c 6 /var/tmp/gate3-squid-seed | od -An -tx1 | tr -d ' \n')
            rm -rf /var/spool/squid
            if [ "$magic" = "fd377a585a00" ]; then
              echo "#{@label}: restoring xz-compressed apt cache seed ($(du -h /var/tmp/gate3-squid-seed | cut -f1))"
              tar -C /var/spool -xpJf /var/tmp/gate3-squid-seed
            else
              echo "#{@label}: restoring uncompressed apt cache seed ($(du -h /var/tmp/gate3-squid-seed | cut -f1))"
              tar -C /var/spool -xpf /var/tmp/gate3-squid-seed
            fi
            chown -R proxy:proxy /var/spool/squid
            rm -f /var/tmp/gate3-squid-seed
          else
            squid -z --foreground || true
          fi
          systemctl enable --now squid
          # Do not declare the box up until squid has SERVED something through
          # itself: `systemctl is-active` says yes while squid is still parsing.
          for i in $(seq 1 30); do
            if curl -sS -o /dev/null -x http://127.0.0.1:#{@cache_port} \
                 http://download.proxmox.com/debian/pve/dists/trixie/Release; then
              echo "#{@label}: apt cache is serving on #{cache_ip}:#{@cache_port}"
              exit 0
            fi
            sleep 2
          done
          echo "#{@label}: squid did not serve a request within 60s" >&2
          tail -20 /var/log/squid/cache.log >&2 || true
          exit 1
        SHELL
    end

    private

    # testrunner.yml `ext.rig` (name, prefix), when the rig has one
    def ext_rig_settings
      f = File.join(@root, "testrunner.yml")
      return {} unless File.exist?(f)
      data = YAML.safe_load(File.read(f), aliases: true) || {}
      (data["ext"] || {})["rig"] || {}
    rescue Psych::Exception => e
      warn "#{File.basename(@root)}: testrunner.yml did not parse (#{e.message}) — pass name:/prefix: explicitly"
      {}
    end
  end
end
