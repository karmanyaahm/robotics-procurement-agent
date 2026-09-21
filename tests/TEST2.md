# Wifi adapter

# Objective

---

- Must work with a **Jetson Orin Nano running JetPack 6.x / Ubuntu 22.04 / ROS 2 Humble**, with real ARM64/Linux compatibility and a sane driver path for JetPack’s kernel.
- **Physical constraint:** The Jetson is mounted inside the robot chassis, which behaves like a Faraday cage. A radio inside the robot is only useful if its antennas can be routed outside; an externally positioned USB dongle is more convenient.
- **Required topology:** upstream IoT Wi-Fi → Jetson → Jetson-hosted 6 GHz hotspot → user laptop/control station.
- The Jetson must simultaneously join the existing IoT Wi-Fi for uplink and host a separate 6 GHz AP. The uplink may use 2.4 or 5 GHz, but it must be wireless; 6 GHz is required for the high-bandwidth ROS side.
- The main performance path is **Jetson/robot ↔ 6 GHz hotspot ↔ laptop/control station**, so reliability and throughput on the 6 GHz AP matter more than uplink performance.
- Preferred architectures, in order:
    1. **One external USB dongle** that can reliably run uplink STA + 6 GHz AP concurrently.
    2. **One M.2 Wi-Fi card with antennas routed outside the cage** that can provide the same concurrency.
    3. Two radios: an external uplink adapter or Wi-Fi bridge plus a separate external 6 GHz AP-capable adapter.
- The user-facing hotspot must be hosted by the Jetson. A router that replaces the Jetson hotspot does not satisfy the requirement.
- The selected unified USB or M.2 device must support **actual 6 GHz AP/hostapd mode** and simultaneous STA + AP operation in its Linux driver. Tri-band client support alone is insufficient.
- A travel router may solve only the uplink side by joining the IoT Wi-Fi and feeding the Jetson over Ethernet. Ethernet between that device and the Jetson is fine; Ethernet from the infrastructure is not.
- Cost matters, but avoid fragile or unsupported Linux drivers.

## Deliverables

- For each viable route, provide:
    - **exact products**
    - total hardware cost
    - current **Ideal Price** + **Amazon price**
    - whether Amazon pricing is good or overpriced
    - pros and cons
    - Jetson/Linux driver caveats
    - confirmation of 6 GHz AP capability
    - any additional hardware required
    - overall setup/networking complexity.

## Resources

links or videos or smth idk

# Progress

---

## September 17, 2026

### Progress

Apparently TP-Link is banned from UT purchasing so our previous plans are cooked

### Participants

@Karmanyaah @Allen Ai 

### Future Steps
