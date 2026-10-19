# Load a Vagrantfile with a stub `Vagrant` that RECORDS every configuration call, and
# print the log as JSON: [path, method, args, kwargs] per call. Nothing is created and
# vagrant itself is never loaded, so this runs on any CI runner with a ruby.
# Shared, byte for byte, by proxmox-gate3 and proxmox-lab-opnsense.
#
# Usage: ruby record.rb <Vagrantfile>
require "json"
class Rec
  def initialize(path, log) = (@path, @log = path, log)
  def method_missing(name, *args, **kw, &blk)
    @log << [@path, name.to_s, args.map(&:to_s), kw.to_h { |k, v| [k.to_s, v.to_s] }]
    child = Rec.new("#{@path}.#{name}#{args.first.is_a?(String) || args.first.is_a?(Symbol) ? "[#{args.first}]" : ""}", @log)
    if blk
      blk.arity == 2 ? blk.call(child, Rec.new("#{@path}.override", @log)) : blk.call(child)
    end
    child
  end
  def respond_to_missing?(*) = true
  def to_s = "<#{@path}>"
end
LOG = []
module Vagrant
  def self.configure(_v) = yield(Rec.new("config", LOG))
end
load ARGV[0]
puts JSON.pretty_generate(LOG)
