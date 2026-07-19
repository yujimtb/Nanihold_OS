@{
    SchemaVersion = 2
    SwitchName = 'mcp-internal'
    Gateway = '172.31.100.1'
    ExistingMcpGateway = '172.31.100.10'
    PrefixLength = 24
    DnsServers = @('1.1.1.1', '8.8.8.8')
    VmRoot = 'D:\Hyper-V'
    Availability = @{
        RpoSeconds = 0
        RtoSeconds = 300
    }
    Clusters = @(
        @{
            Name = 'nanihold'
            Vip = '172.31.100.20'
            PodCidr = '10.50.0.0/16'
            ServiceCidr = '10.51.0.0/16'
            Nodes = @('nh-control-a', 'nh-control-b', 'nh-control-q')
        },
        @{
            Name = 'lethe'
            Vip = '172.31.100.30'
            PodCidr = '10.60.0.0/16'
            ServiceCidr = '10.61.0.0/16'
            Nodes = @('lethe-a', 'lethe-b', 'lethe-q')
        }
    )
    Nodes = @(
        @{
            Name = 'nh-control-a'
            Cluster = 'nanihold'
            Address = '172.31.100.21'
            Role = 'control'
            Cpu = 4
            StartupMemoryGiB = 8
            MinimumMemoryGiB = 4
            MaximumMemoryGiB = 12
            DiskGiB = 64
        },
        @{
            Name = 'nh-control-b'
            Cluster = 'nanihold'
            Address = '172.31.100.22'
            Role = 'control'
            Cpu = 4
            StartupMemoryGiB = 8
            MinimumMemoryGiB = 4
            MaximumMemoryGiB = 12
            DiskGiB = 64
        },
        @{
            Name = 'nh-control-q'
            Cluster = 'nanihold'
            Address = '172.31.100.23'
            Role = 'quorum'
            Cpu = 2
            StartupMemoryGiB = 4
            MinimumMemoryGiB = 2
            MaximumMemoryGiB = 6
            DiskGiB = 48
        },
        @{
            Name = 'lethe-a'
            Cluster = 'lethe'
            Address = '172.31.100.31'
            Role = 'data'
            Cpu = 8
            StartupMemoryGiB = 16
            MinimumMemoryGiB = 8
            MaximumMemoryGiB = 24
            DiskGiB = 192
        },
        @{
            Name = 'lethe-b'
            Cluster = 'lethe'
            Address = '172.31.100.32'
            Role = 'data'
            Cpu = 8
            StartupMemoryGiB = 16
            MinimumMemoryGiB = 8
            MaximumMemoryGiB = 24
            DiskGiB = 192
        },
        @{
            Name = 'lethe-q'
            Cluster = 'lethe'
            Address = '172.31.100.33'
            Role = 'quorum'
            Cpu = 2
            StartupMemoryGiB = 8
            MinimumMemoryGiB = 4
            MaximumMemoryGiB = 10
            DiskGiB = 64
        }
    )
}
