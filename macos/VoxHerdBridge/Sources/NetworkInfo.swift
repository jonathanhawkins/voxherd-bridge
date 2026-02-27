import Foundation
import Darwin

enum NetworkInfo {
    /// Returns the primary LAN IPv4 address (en0 WiFi preferred, then en1 Ethernet).
    static func primaryLANAddress() -> String? {
        let addrs = localIPv4Addresses()
        // Prefer en0 (WiFi), then en1 (Ethernet), then any non-loopback
        if let en0 = addrs.first(where: { $0.interface == "en0" }) { return en0.address }
        if let en1 = addrs.first(where: { $0.interface == "en1" }) { return en1.address }
        return addrs.first?.address
    }

    /// Returns the Tailscale IPv4 address if present (100.64.0.0/10 CGNAT range on utun interfaces).
    static func tailscaleAddress() -> String? {
        localIPv4Addresses()
            .filter { $0.interface.hasPrefix("utun") }
            .first(where: { isTailscaleIP($0.address) })?
            .address
    }

    /// Enumerate all non-loopback IPv4 addresses with their interface names.
    static func localIPv4Addresses() -> [(interface: String, address: String)] {
        var results: [(interface: String, address: String)] = []
        var ifaddr: UnsafeMutablePointer<ifaddrs>?

        guard getifaddrs(&ifaddr) == 0, let firstAddr = ifaddr else { return results }
        defer { freeifaddrs(ifaddr) }

        var current: UnsafeMutablePointer<ifaddrs>? = firstAddr
        while let addr = current {
            defer { current = addr.pointee.ifa_next }

            guard addr.pointee.ifa_addr.pointee.sa_family == UInt8(AF_INET) else { continue }

            let name = String(cString: addr.pointee.ifa_name)

            var hostname = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            let result = getnameinfo(
                addr.pointee.ifa_addr, socklen_t(addr.pointee.ifa_addr.pointee.sa_len),
                &hostname, socklen_t(hostname.count),
                nil, 0, NI_NUMERICHOST
            )
            guard result == 0 else { continue }

            let ip = String(cString: hostname)

            // Skip loopback and link-local
            if ip.hasPrefix("127.") || ip.hasPrefix("169.254.") { continue }

            results.append((interface: name, address: ip))
        }

        return results
    }

    /// Check if an IP is in the Tailscale CGNAT range (100.64.0.0/10).
    private static func isTailscaleIP(_ ip: String) -> Bool {
        let parts = ip.split(separator: ".").compactMap { UInt8($0) }
        guard parts.count == 4 else { return false }
        // 100.64.0.0/10 means first octet is 100, second octet 64-127
        return parts[0] == 100 && (parts[1] & 0xC0) == 64
    }
}
