# NAS throughput dashboard addition

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
