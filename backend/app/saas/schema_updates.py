"""Idempotent compatibility updates; never remove historical accounting data."""
import re
from sqlalchemy import inspect, text


def _detach_sqlite_reference(connection):
    name = 'saas_workflow_runs'
    inspector = inspect(connection)
    if name not in inspector.get_table_names():
        return
    columns = {item['name']: item for item in inspector.get_columns(name)}
    if 'reservation_id' not in columns:
        return
    references = [fk for fk in inspector.get_foreign_keys(name)
                  if 'reservation_id' in fk['constrained_columns']]
    if columns['reservation_id']['nullable'] and not references:
        return
    # Preserve the actual DDL: reflection alone can lose custom collation and
    # generated-column semantics. Split only top-level column definitions.
    ddl = connection.scalar(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"), {'name': name})
    start, end = ddl.index('('), ddl.rfind(')')
    body, definitions, current, depth, quote = ddl[start + 1:end], [], [], 0, None
    index = 0
    while index < len(body):
        char = body[index]
        current.append(char)
        if quote:
            close = ']' if quote == '[' else quote
            if char == close:
                if index + 1 < len(body) and body[index + 1] == close:
                    current.append(body[index + 1]); index += 1
                else:
                    quote = None
        elif char in ('"', "'", '`', '['):
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif char == ',' and depth == 0:
            definitions.append(''.join(current[:-1])); current = []
        index += 1
    definitions.append(''.join(current))
    revised = []
    for definition in definitions:
        # The shipped legacy schema has a single-column reference. Keep other
        # foreign keys, unique constraints, checks and deployment customizations.
        first = definition.strip().split()[0].strip('"`[]').lower()
        if first == 'reservation_id':
            definition = re.sub(r'\bNOT\s+NULL\b', '', definition, flags=re.I)
            definition = re.sub(r'\s+REFERENCES\s+[^\s(]+\s*\([^)]*\)'
                r'(?:\s+ON\s+(?:DELETE|UPDATE)\s+(?:SET\s+NULL|SET\s+DEFAULT|NO\s+ACTION|CASCADE|RESTRICT))*',
                '', definition, flags=re.I)
        elif re.search(r'FOREIGN\s+KEY\s*\(\s*["`\[]?reservation_id["`\]]?\s*\)', definition, re.I):
            continue
        revised.append(definition)
    temporary = 'zhixu_workflow_runs_upgrade'
    objects = connection.execute(text("SELECT sql FROM sqlite_master WHERE tbl_name=:name "
        "AND type IN ('index','trigger') AND sql IS NOT NULL"), {'name': name}).scalars().all()
    connection.exec_driver_sql('CREATE TABLE ' + temporary + ' (' + ','.join(revised) + ')' + ddl[end + 1:])
    quote_identifier = connection.dialect.identifier_preparer.quote
    writable = ','.join(quote_identifier(column['name']) for column in columns.values() if not column.get('computed'))
    connection.execute(text(f'INSERT INTO {temporary} ({writable}) SELECT {writable} FROM {name}'))
    count = connection.scalar(text('SELECT count(*) FROM saas_workflow_runs'))
    if count != connection.scalar(text('SELECT count(*) FROM zhixu_workflow_runs_upgrade')):
        raise RuntimeError('Workflow schema copy was incomplete')
    connection.execute(text('DROP TABLE saas_workflow_runs'))
    connection.execute(text('ALTER TABLE zhixu_workflow_runs_upgrade RENAME TO saas_workflow_runs'))
    for statement in objects:
        connection.exec_driver_sql(statement)



async def upgrade_schema(engine):
    async with engine.connect() as conn:
        if engine.dialect.name == 'sqlite':
            await conn.exec_driver_sql('PRAGMA foreign_keys=OFF')
            await conn.commit()
            try:
                # An explicit transaction makes DDL and row copying atomic on
                # sqlite3 versions that otherwise use legacy autocommit for DDL.
                await conn.exec_driver_sql('BEGIN IMMEDIATE')
                await conn.run_sync(_detach_sqlite_reference)
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
            finally:
                await conn.exec_driver_sql('PRAGMA foreign_keys=ON')
                await conn.commit()
        elif engine.dialect.name == 'postgresql':
            async with conn.begin():
                tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
                if 'saas_workflow_runs' in tables:
                    fks = await conn.run_sync(lambda c: inspect(c).get_foreign_keys('saas_workflow_runs'))
                    quote = engine.dialect.identifier_preparer.quote
                    for fk in fks:
                        if 'reservation_id' in fk['constrained_columns']:
                            await conn.execute(text('ALTER TABLE saas_workflow_runs DROP CONSTRAINT ' + quote(fk['name'])))
                    await conn.execute(text('ALTER TABLE saas_workflow_runs ALTER COLUMN reservation_id DROP NOT NULL'))
        async with conn.begin():
            if not await conn.run_sync(lambda c: inspect(c).has_table('saas_knowledge_configs')):
                return
            columns = await conn.run_sync(lambda c: inspect(c).get_columns('saas_knowledge_configs'))
            if 'parser_config' not in {column['name'] for column in columns}:
                await conn.execute(text("ALTER TABLE saas_knowledge_configs ADD COLUMN parser_config JSON NOT NULL DEFAULT '{}'"))
