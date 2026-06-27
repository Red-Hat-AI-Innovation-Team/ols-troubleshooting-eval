import psycopg2
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass
class Table:
    table_name: str
    references: list[str] | None = None


def get_seeding_order(conn) -> list[Table]:
    cur = conn.cursor()

    cur.execute("""
      SELECT table_name
      FROM information_schema.tables
      WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    """)
    all_tables = {row[0] for row in cur.fetchall()}

    cur.execute("""
      SELECT
        tc.table_name AS child_table,
        ccu.table_name AS parent_table
      FROM information_schema.table_constraints tc
      JOIN information_schema.referential_constraints rc
        ON tc.constraint_name = rc.constraint_name
        AND tc.constraint_schema = rc.constraint_schema
      JOIN information_schema.constraint_column_usage ccu
        ON rc.unique_constraint_name = ccu.constraint_name
        AND rc.unique_constraint_schema = ccu.constraint_schema
      WHERE tc.constraint_type = 'FOREIGN KEY'
        AND tc.table_schema = 'public'
    """)

    dependencies = defaultdict(set)
    for child, parent in cur.fetchall():
        dependencies[child].add(parent)

    cur.close()

    graph = defaultdict(list)
    in_degree = {t: 0 for t in all_tables}

    for child, parents in dependencies.items():
        for parent in parents:
            graph[parent].append(child)
            in_degree[child] += 1

    queue = deque(sorted(t for t in all_tables if in_degree[t] == 0))
    order = []

    while queue:
        table = queue.popleft()
        order.append(table)
        for child in sorted(graph[table]):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(order) != len(all_tables):
        raise RuntimeError("Cycle detected in FK dependencies")

    return [
        Table(
            table_name=t,
            references=sorted(dependencies[t]) if dependencies[t] else None,
        )
        for t in order
    ]


if __name__ == "__main__":
    conn = psycopg2.connect("host=127.0.0.1 port=5433 dbname=openshift_cluster user=postgres")
    tables = get_seeding_order(conn)
    conn.close()
    for i, t in enumerate(tables):
        refs = f" -> {t.references}" if t.references else ""
        print(f"{i+1}. {t.table_name}{refs}")
