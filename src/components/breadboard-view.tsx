import { useEffect, useMemo, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { ComponentRegistry } from '../../vendor/velxio/frontend/src/services/ComponentRegistry'
import { breadboardGroupKey, isBreadboard } from '../../vendor/velxio/frontend/src/utils/breadboardNets'
import { readPinInfo } from '../../vendor/velxio/frontend/src/utils/readPinInfo'
import '../../vendor/velxio/frontend/src/elements-register'
import type { HardwarePin, HardwareProject, WireEndpoint } from '../lib/hardware'
import './breadboard-view.css'

export type BreadboardViewProps = { project: HardwareProject; onClose: () => void }
export type BreadboardNet = { id: string; endpoints: WireEndpoint[]; wireIds: string[] }
export type BreadboardAssignment = {
  net: BreadboardNet; group: string; holes: string[]
  endpoints: { endpoint: WireEndpoint; hole: string }[]
}
export type BreadboardLayout = { assignments: BreadboardAssignment[]; reason: string | null; capacity: number }

const compare = (a: string, b: string) => a < b ? -1 : a > b ? 1 : 0
const endpointKey = (endpoint: WireEndpoint) => JSON.stringify([endpoint.component, endpoint.pin])
const endpointLabel = (endpoint: WireEndpoint) => `${endpoint.component}.${endpoint.pin}`

function buildBreadboardNets(project: HardwareProject): BreadboardNet[] {
  const parents = new Map<string, string>()
  const endpoints = new Map<string, WireEndpoint>()
  const parts = new Map(project.components.map(part => [part.id, part]))
  const internal = new Map<string, string>()
  const find = (key: string): string => {
    let root = key
    while (parents.get(root) !== root) root = parents.get(root)!
    let current = key
    while (current !== root) {
      const next = parents.get(current)!
      parents.set(current, root)
      current = next
    }
    return root
  }
  const union = (a: string, b: string) => {
    const left = find(a)
    const right = find(b)
    if (left !== right) parents.set(right, left)
  }
  for (const wire of project.wires) {
    for (const endpoint of [wire.from, wire.to]) {
      const key = endpointKey(endpoint)
      if (parents.has(key)) continue
      parents.set(key, key)
      endpoints.set(key, { ...endpoint })
      const part = parts.get(endpoint.component)
      const group = part ? breadboardGroupKey(part.type, endpoint.pin) : null
      if (group !== null) {
        const internalKey = JSON.stringify([endpoint.component, group])
        const previous = internal.get(internalKey)
        if (previous !== undefined) union(key, previous)
        else internal.set(internalKey, key)
      }
    }
    union(endpointKey(wire.from), endpointKey(wire.to))
  }
  const groups = new Map<string, WireEndpoint[]>()
  for (const [key, endpoint] of endpoints) {
    const root = find(key)
    const members = groups.get(root) ?? []
    members.push(endpoint)
    groups.set(root, members)
  }
  return [...groups.entries()]
    .map(([root, members]) => ({
      root,
      endpoints: members.sort((a, b) => compare(endpointKey(a), endpointKey(b))),
      wireIds: project.wires.filter(wire => find(endpointKey(wire.from)) === root).map(wire => wire.id).sort(compare),
    }))
    .sort((a, b) => compare(endpointKey(a.endpoints[0]), endpointKey(b.endpoints[0])))
    .map((group, index) => ({ id: `Net ${index + 1}`, endpoints: group.endpoints, wireIds: group.wireIds }))
}

function allocateBreadboardNets(nets: BreadboardNet[], pins: readonly HardwarePin[]): BreadboardLayout {
  const groups = new Map<string, string[]>()
  for (const pin of pins) {
    if (!/^\d+[tb]\.[a-j]$/.test(pin.name)) continue
    const group = breadboardGroupKey('breadboard-mini', pin.name)
    if (group === null) continue
    const holes = groups.get(group) ?? []
    if (!holes.includes(pin.name)) holes.push(pin.name)
    groups.set(group, holes)
  }
  const available = [...groups.entries()].sort(([a], [b]) => {
    const column = Number(a.slice(3, -1)) - Number(b.slice(3, -1))
    return column || compare(a, b)
  }).map(([group, holes]) => ({ group, holes: holes.sort(compare) }))
  const capacity = available.length
  const fail = (reason: string): BreadboardLayout => ({ assignments: [], reason, capacity })
  if (!capacity) return fail('No layout: the registered mini breadboard did not report any verified terminal groups.')
  if (nets.length > capacity) return fail(`No layout: ${nets.length} nets require distinct terminal groups, but this registered mini breadboard has only ${capacity}.`)
  const remaining = [...available]
  const assignments: BreadboardAssignment[] = []
  // Largest nets first avoids consuming a larger strip when reported group sizes differ.
  const allocationOrder = [...nets].sort((a, b) => b.endpoints.length - a.endpoints.length || compare(a.id, b.id))
  for (const net of allocationOrder) {
    const index = remaining.findIndex(group => group.holes.length >= net.endpoints.length)
    if (index < 0) return fail(`No layout: ${net.id} needs ${net.endpoints.length} separate contacts; no remaining verified terminal group can hold them. No partial layout is shown.`)
    const [target] = remaining.splice(index, 1)
    assignments.push({
      net, group: target.group, holes: target.holes,
      endpoints: net.endpoints.map((endpoint, index) => ({ endpoint: { ...endpoint }, hole: target.holes[index] })),
    })
  }
  return { assignments: nets.map(net => assignments.find(assignment => assignment.net === net)!), reason: null, capacity }
}

type Topology = { project: HardwareProject; pins: Record<string, HardwarePin[]>; error: string | null }

export function BreadboardView({ project, onClose }: BreadboardViewProps) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [topology, setTopology] = useState<Topology | null>(null)
  const boards = project.components.filter(part => isBreadboard(part.type))
  const actual = boards.length > 0
  const nets = useMemo(() => buildBreadboardNets(project), [project])
  const currentTopology = topology?.project === project ? topology : null
  const layout = useMemo(() => currentTopology && !actual
    ? allocateBreadboardNets(nets, currentTopology.pins['breadboard-mini'] ?? []) : null,
  [currentTopology, actual, nets])
  const parts = new Map(project.components.map(part => [part.id, part]))

  useEffect(() => {
    const element = dialog.current
    element?.showModal()
    return () => element?.close()
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const registry = ComponentRegistry.getInstance()
      await registry.load()
      const targets = project.components.filter(part => isBreadboard(part.type))
      const pins: Record<string, HardwarePin[]> = {}
      if (!targets.length) {
        const metadata = registry.getById('breadboard-mini')
        if (!metadata || !customElements.get(metadata.tagName)) throw new Error('Registered mini breadboard is unavailable.')
        const element = document.createElement(metadata.tagName)
        Object.assign(element, metadata.defaultValues)
        pins['breadboard-mini'] = (readPinInfo<HardwarePin>(element) ?? []).map(pin => ({ ...pin }))
      } else {
        for (const part of targets) {
          const metadata = registry.getById(part.type)
          if (!metadata || !customElements.get(metadata.tagName)) continue
          const element = document.createElement(metadata.tagName)
          Object.assign(element, metadata.defaultValues, part.properties)
          pins[part.id] = (readPinInfo<HardwarePin>(element) ?? []).map(pin => ({ ...pin }))
        }
      }
      if (active) setTopology({ project, pins, error: null })
    })().catch(error => {
      if (active) setTopology({ project, pins: {}, error: error instanceof Error ? error.message : 'Breadboard topology unavailable.' })
    })
    return () => { active = false }
  }, [project])

  const storedEndpoint = (endpoint: WireEndpoint) => {
    const part = parts.get(endpoint.component)
    if (!part) return <><code>{endpointLabel(endpoint)}</code><small>Component missing from saved project</small></>
    if (!isBreadboard(part.type)) return <code>{endpointLabel(endpoint)}</code>
    const group = breadboardGroupKey(part.type, endpoint.pin)
    const reported = currentTopology?.pins[part.id]
    const verified = reported?.some(pin => pin.name === endpoint.pin)
    return <><code>{endpointLabel(endpoint)}</code><small>Hole {endpoint.pin} · {group === null ? 'No upstream group' : `Group ${group}`}</small><small>{!currentTopology ? 'Checking registered hole…' : verified ? 'Hole verified against registered pinInfo' : 'Hole unverified or absent from registered pinInfo; stored endpoint unchanged'}</small></>
  }

  return <dialog ref={dialog} className="breadboard-view" aria-labelledby="breadboard-view-title" aria-describedby="breadboard-view-description" onCancel={onClose}>
    <header className="breadboard-view-header">
      <h2 id="breadboard-view-title">Connections through a breadboard</h2>
      <button type="button" autoFocus aria-label="Close breadboard view" onClick={onClose}><X size={18} /></button>
    </header>
    <div className="breadboard-view-body">
      <p className="breadboard-view-badge">{actual ? 'Actual stored circuit connections · read-only' : 'Proposed layout · not the saved circuit'}</p>
      <p id="breadboard-view-description">{actual
        ? 'These are the actual wire records stored in this project, not a claim about existing physical wiring. Breadboard endpoints and upstream internal-group labels are shown without rerouting or adding wires.'
        : 'This is an unsaved electrical connection proposal for the registered Breadboard Mini. Each net gets a distinct terminal group, with one verified hole per endpoint. The original circuit is not changed.'}</p>
      <p className="breadboard-view-caution">Contact occupancy and component seating are not planned. This view does not establish physical safety, wire routing, or a manufacturing guarantee. No firmware is generated, and nothing is saved.</p>
      {!currentTopology && <p role="status">Reading registered breadboard pinInfo…</p>}
      {currentTopology?.error && <p role="status">{currentTopology.error} {actual ? 'Stored connections remain visible; hole verification is unavailable.' : 'No layout is shown.'}</p>}
      {!project.wires.length && <p role="status">No explicit wires are stored. No connections or breadboard assignments are invented.</p>}
      {!actual && layout?.reason && <p className="breadboard-view-caution" role="status">{layout.reason}</p>}
      {!actual && layout && !layout.reason && nets.length > 0 && <>
        <h3>Proposed net assignments</h3>
        <p>{nets.length} nets · {layout.capacity} verified terminal groups available. Diagram strips show electrical groups, not physical routing or component placement.</p>
        <div className="breadboard-view-diagram">
          <svg viewBox={`0 0 520 ${layout.assignments.length * 38 + 12}`} role="img" aria-label="Proposed breadboard terminal groups, not physical wiring">
            {layout.assignments.map((assignment, index) => <g key={assignment.net.id} transform={`translate(0 ${index * 38 + 22})`}>
              <text x="8" y="4">{assignment.net.id} → {assignment.group}</text>
              <line x1="220" y1="0" x2={220 + (assignment.holes.length - 1) * 60} y2="0" />
              {assignment.holes.map((hole, holeIndex) => <g key={hole} transform={`translate(${220 + holeIndex * 60} 0)`}><circle r="4" /><text y="16" textAnchor="middle">{hole}</text></g>)}
            </g>)}
          </svg>
        </div>
      </>}
      {nets.length > 0 && <section aria-label={actual ? 'Stored electrical nets' : 'Original nets and proposed assignments'}>
        <h3>{actual ? 'Stored electrical nets' : 'Original nets → proposed terminal groups'}</h3>
        <div className="breadboard-view-table-wrap"><table>
          <thead><tr><th>Net / stored wires</th><th>All explicit endpoints</th><th>{actual ? 'Stored breadboard groups' : 'Proposed assignment (unsaved)'}</th></tr></thead>
          <tbody>{nets.map(net => {
            const assignment = layout?.assignments.find(item => item.net.id === net.id)
            const storedGroups = [...new Set(net.endpoints.flatMap(endpoint => {
              const part = parts.get(endpoint.component)
              const group = part && isBreadboard(part.type) ? breadboardGroupKey(part.type, endpoint.pin) : null
              return group === null ? [] : [`${endpoint.component} / ${group}`]
            }))]
            return <tr key={net.id}><td><strong>{net.id}</strong><small>{net.wireIds.join(', ')}</small></td>
              <td>{net.endpoints.map(endpoint => <code className="breadboard-view-endpoint" key={endpointKey(endpoint)}>{endpointLabel(endpoint)}</code>)}</td>
              <td>{actual ? storedGroups.length ? storedGroups.map(group => <code className="breadboard-view-endpoint" key={group}>{group}</code>) : 'No stored breadboard group'
                : assignment ? <><strong>→ {assignment.group}</strong>{assignment.endpoints.map(({ endpoint, hole }) => <small key={endpointKey(endpoint)}><code>{endpointLabel(endpoint)} → {hole}</code></small>)}</> : 'No layout assigned'}</td>
            </tr>
          })}</tbody>
        </table></div>
        <p>Nets join explicit wires and matching internal groups of the same original component using upstream breadboardGroupKey. Different component IDs do not share internal groups.</p>
      </section>}
      <section aria-label="Actual stored wire paths">
        <h3>Actual stored wire paths (unchanged)</h3>
        <div className="breadboard-view-table-wrap"><table>
          <thead><tr><th>Wire</th><th>From</th><th>To</th></tr></thead>
          <tbody>{project.wires.map((wire, index) => <tr key={`${wire.id}:${index}`}><td><code>{wire.id}</code><small>Color: {wire.color}</small></td><td>{storedEndpoint(wire.from)}</td><td>{storedEndpoint(wire.to)}</td></tr>)}</tbody>
        </table></div>
      </section>
    </div>
    <footer className="breadboard-view-footer"><button type="button" onClick={onClose}>Close preview</button></footer>
  </dialog>
}
