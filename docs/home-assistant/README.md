# NAS and WAN throughput dashboard additions

`dash-nas.yaml` is the complete updated view saved to
`/homeassistant/dashboards/views/dash-nas.yaml` in Home Assistant.
`nas-throughput.yaml` contains just the new cards for use in other views. Append
its sequence item to a view's existing `cards` list. Both use the existing Bubble
Card, ApexCharts Card and card-mod resources.

For a `custom:grid-layout` view, add a final row to each `grid-template-areas`
block, preserving its existing rows:

```yaml
# Four columns (desktop)
"throughput throughput throughput throughput"

# Two columns (tablet)
"throughput throughput"

# One column (phone)
"throughput"
```

Use **Refresh** in the Home Assistant dashboard menu after saving YAML. Sensors
appear automatically after updating the Eero container with MQTT discovery
enabled. The first numeric reading needs two successful polls, about 30 seconds
apart. Change the two entity IDs if Home Assistant assigned different IDs or
the NAS has a different name. History starts when the sensors are created and
requires Home Assistant Recorder to retain these entities.

The values include local network transfers. Receive means **to the NAS** and
transmit means **from the NAS**. Cards show Mbit/s; the one-hour chart averages
samples into one-minute buckets and leaves unavailable periods as gaps.

`wan-throughput.yaml` adds one compact card with total WAN download and upload.
The full view places it in the existing `network` grid area. Change its two
entity IDs to match your Eero network's name if necessary. The MQTT network
device now discovers **WAN download** and **WAN upload**, separately from the
latest speed-test sensors. Eero's live usage excludes local LAN transfers.

With MQTT enabled, the existing collector requests a live report at most once
per minute, waits 12 seconds for counters, then reuses its device-list read.
Normal collection defaults to about 72 seconds including this wait. Only the
two aggregate rates are published. Missing/incomplete usage, failed reporting,
bridge mode and samples older than 90 seconds show unknown. No speed test is
run, and no new credentials or per-device Home Assistant entities are needed.
