import type { NodeStatus, TopologyNode } from "./types";

export const NODE_WIDTH = 224;
export const NODE_HEIGHT = 158;
export const NODE_GAP_X = 24;
export const LEVEL_GAP = 58;
export const STAGE_PADDING = 32;

const ROLE_ORDER: Record<string, number> = {
  S5_POLICY: 0,
  S4_SCANNER: 1,
  S3_ALLOCATOR: 2,
  S3STAR_AUDITOR: 3,
  S2_COORDINATOR: 4,
  S1_WORKER: 5,
};

export interface TopologyLayoutNode {
  node: TopologyNode;
  x: number;
  y: number;
  depth: number;
}

export interface TopologyLayoutEdge {
  source: TopologyLayoutNode;
  target: TopologyLayoutNode;
  path: string;
}

export interface TopologyLayout {
  nodes: TopologyLayoutNode[];
  edges: TopologyLayoutEdge[];
  width: number;
  height: number;
}

function nodeOrder(a: TopologyNode, b: TopologyNode) {
  return (ROLE_ORDER[a.role] ?? 99) - (ROLE_ORDER[b.role] ?? 99)
    || a.role.localeCompare(b.role)
    || a.node_id.localeCompare(b.node_id);
}

/**
 * Tidy, top-down layout for a topology forest.
 *
 * The projection normally contains one S5 root, but keeping the algorithm
 * forest-aware makes recursive u-VSM topologies and temporarily incomplete
 * event logs render without a special-case role tree.
 */
export function layoutTopology(nodes: TopologyNode[]): TopologyLayout {
  const byId = new Map(nodes.map((node) => [node.node_id, node]));
  const children = new Map<string, TopologyNode[]>();
  const roots: TopologyNode[] = [];

  for (const node of nodes) {
    if (node.parent_id && byId.has(node.parent_id) && node.parent_id !== node.node_id) {
      const siblings = children.get(node.parent_id) ?? [];
      siblings.push(node);
      children.set(node.parent_id, siblings);
    } else {
      roots.push(node);
    }
  }
  for (const siblings of children.values()) siblings.sort(nodeOrder);
  roots.sort(nodeOrder);

  const placed = new Map<string, { centerX: number; depth: number }>();
  const cursor = { x: STAGE_PADDING + NODE_WIDTH / 2 };
  const visit = (node: TopologyNode, depth: number, path: Set<string>) => {
    if (placed.has(node.node_id) || path.has(node.node_id)) return;
    const nextPath = new Set(path).add(node.node_id);
    const descendants = (children.get(node.node_id) ?? [])
      .filter((child) => !nextPath.has(child.node_id));
    for (const child of descendants) visit(child, depth + 1, nextPath);

    const childPositions = descendants
      .map((child) => placed.get(child.node_id))
      .filter((position): position is { centerX: number; depth: number } => Boolean(position));
    const centerX = childPositions.length
      ? (childPositions[0].centerX + childPositions[childPositions.length - 1].centerX) / 2
      : cursor.x;
    if (!childPositions.length) cursor.x += NODE_WIDTH + NODE_GAP_X;
    placed.set(node.node_id, { centerX, depth });
  };

  for (const root of roots) visit(root, 0, new Set());
  // A malformed cycle has no natural root. Keep it visible as a separate
  // component while never producing a recursive render loop.
  for (const node of nodes) {
    if (!placed.has(node.node_id)) visit(node, 0, new Set());
  }

  const layoutNodes = nodes
    .map((node) => {
      const position = placed.get(node.node_id);
      if (!position) throw new Error(`トポロジーのレイアウトに失敗しました: ${node.node_id}`);
      return {
        node,
        x: position.centerX - NODE_WIDTH / 2,
        y: STAGE_PADDING + position.depth * (NODE_HEIGHT + LEVEL_GAP),
        depth: position.depth,
      };
    })
    .sort((a, b) => a.y - b.y || a.x - b.x || nodeOrder(a.node, b.node));
  const layoutById = new Map(layoutNodes.map((item) => [item.node.node_id, item]));
  const edges: TopologyLayoutEdge[] = [];

  for (const target of layoutNodes) {
    if (!target.node.parent_id) continue;
    const source = layoutById.get(target.node.parent_id);
    if (!source) continue;
    const sourceX = source.x + NODE_WIDTH / 2;
    const targetX = target.x + NODE_WIDTH / 2;
    const sourceY = source.y + NODE_HEIGHT;
    const targetY = target.y;
    const middleY = sourceY + Math.max(18, (targetY - sourceY) / 2);
    edges.push({
      source,
      target,
      path: `M ${sourceX} ${sourceY} C ${sourceX} ${middleY}, ${targetX} ${middleY}, ${targetX} ${targetY}`,
    });
  }

  const maxX = layoutNodes.reduce((value, item) => Math.max(value, item.x + NODE_WIDTH), STAGE_PADDING);
  const maxY = layoutNodes.reduce((value, item) => Math.max(value, item.y + NODE_HEIGHT), STAGE_PADDING);
  return {
    nodes: layoutNodes,
    edges,
    width: maxX + STAGE_PADDING,
    height: maxY + STAGE_PADDING,
  };
}

export function nodeStatusClass(status: NodeStatus): string {
  return `node-${status.toLowerCase()}`;
}

export function nodeStatusColor(status: NodeStatus): string {
  switch (status) {
    case "RUNNING": return "#3e9562";
    case "SUSPENDED": return "#c77e31";
    case "WAITING": return "#7a62a8";
    case "FAILED": return "#a54b3d";
    case "IDLE":
    case "CREATED": return "#8e938c";
    case "COMPLETED": return "#3e9b6f";
    case "TERMINATED": return "#a54b3d";
    default: return "#8e938c";
  }
}

export function isRecentlyActive(node: TopologyNode, now = Date.now()): boolean {
  if (node.status !== "RUNNING" || !node.last_activity_at) return false;
  const timestamp = Date.parse(node.last_activity_at);
  return Number.isFinite(timestamp) && now - timestamp < 12_000;
}
