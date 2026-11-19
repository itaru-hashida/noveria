export interface SourceLocation {
  id: string
  file: string
  line: number
  end_line: number | null
  symbol: string | null
  anchor: string
  language: 'python' | 'cython'
  verified: boolean
}

export type NodeType = 'class' | 'module' | 'function' | 'store' | 'kernel' | 'boundary'
export type Language = 'python' | 'cython'

export interface ArchNode {
  id: string
  name: string
  short: string
  type: NodeType
  domain: string
  language: Language
  responsibility: string
  owner: string | null
  source_location: string
  tags: string[]
}

export type EdgeKind = 'calls' | 'writes' | 'owns' | 'binds' | 'callback'

export interface ArchEdge {
  id: string
  source: string
  target: string
  kind: EdgeKind
  label: string
  confidence: 'confirmed' | 'unknown'
  source_location: string
  note: string | null
}

export interface Domain {
  id: string
  title: string
  description: string
  color: string
}

export interface ScenarioStep {
  phase: string
  node: string
  call: string
  condition: string | null
  language: Language
  note: string | null
  source_location: string
}

export interface ScenarioVariant {
  id: string
  title: string
  summary: string
  steps: ScenarioStep[]
  divergences: string[]
}

export interface Scenario {
  id: string
  title: string
  description: string
  variants: ScenarioVariant[]
}

export interface Owner {
  id: string
  title: string
  state: string
  storage: string
  owner_node: string
  fallback_node: string | null
  write_path: string
  readers: string[]
  writers: string[]
  direct_writes: string[]
  callbacks: string[]
  confidence: 'confirmed' | 'unknown'
  notes: string
  evidence: string[]
}

export interface Violation {
  id: string
  title: string
  kind: string
  severity: 'high' | 'medium' | 'low' | 'info'
  state: string
  description: string
  mitigation: string | null
  related_nodes: string[]
  confidence: 'confirmed' | 'unknown'
  source_location: string
}

export interface ArchitectureMeta {
  generated_at: string
  generator: string
  repository: string
  branch: string
  commit: string
  remote_commit: string
  python_files: string[]
  cython_files: string[]
  evidence_tests: string[]
  native_extensions_built: boolean
  native_extension_count: number
  scope_note: string
  limitations: string[]
}

export interface Architecture {
  meta: ArchitectureMeta
  domains: Domain[]
  nodes: ArchNode[]
  edges: ArchEdge[]
  scenarios: Scenario[]
  owners: Owner[]
  violations: Violation[]
  source_locations: SourceLocation[]
  stats: {
    nodes: number
    edges: number
    domains: number
    owners: number
    violations: number
    source_locations: number
    dangling_nodes: string[]
  }
}
