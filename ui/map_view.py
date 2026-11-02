"""Географическая карта: координаты клеток, политические слои и выбор Plotly."""
import plotly.graph_objects as go

PALETTE=['#63b5af','#e7ae67','#9a98d5','#d77479','#8fbe69','#65a2d1']
TERRAIN={'water':'#264b70','plain':'#b7bf78','forest':'#3e795f','mountain':'#8d8d99','river':'#579ec5'}
LABELS={'water':'Море','plain':'Равнина','forest':'Лес','mountain':'Горы','river':'Река'}


def map_figure(tiles,snapshot,layer='Государства',selected_state=None,route_path=None,populations=None):
    index={e['id']:e for e in snapshot['entities']}; links=snapshot['links']
    owners={r['target']:r['source'] for r in links if r['kind']=='owns'}
    states=sorted(e['id'] for e in index.values() if e['kind']=='State')
    provinces=sorted(e['id'] for e in index.values() if e['kind']=='Province')
    colors={i:PALETTE[n%len(PALETTE)] for n,i in enumerate(states)}
    for n,p in enumerate(provinces): colors[p]=f'hsl({n*137%360},45%,56%)'
    by_xy={(t['x'],t['y']):t for t in tiles}; by_id={t['id']:t for t in tiles}
    groups={}
    for t in tiles:
        pid=t['province_id']; sid=owners.get(pid)
        label=LABELS[t['terrain']]; color=TERRAIN[t['terrain']]
        if t['passable']:
            if layer=='Государства': label=index[sid]['name']; color=colors[sid]
            elif layer=='Провинции': label=index[pid]['name']; color=colors[pid]
            elif layer=='Ресурсы': label=', '.join(t['resources']); color={'зерно':'#cbbd64','лес':'#458365','железо':'#92929d','рыба':'#63afcc'}.get(label,'#ccc')
            elif layer=='Дороги': label='Дорога' if t['road'] else LABELS[t['terrain']]; color='#efbd63' if t['road'] else color
            elif layer=='Население':
                value=(populations or {}).get(pid,0); label=index[pid]['name']; color=f'hsl(24,60%,{max(25,80-min(value/2000,50))}%)'
        groups.setdefault((label,color),[]).append(t)
    fig=go.Figure()
    for (label,color),cells in groups.items():
        fig.add_trace(go.Scatter(x=[t['x'] for t in cells],y=[t['y'] for t in cells],mode='markers',name=label,
            marker=dict(symbol='square',size=15,color=color,opacity=[1 if not selected_state or owners.get(t['province_id'])==selected_state else .45 for t in cells]),
            customdata=[t['id'] for t in cells],hovertext=[f'({t["x"]}, {t["y"]}) · {LABELS[t["terrain"]]}<br>{index.get(t["province_id"],{}).get("name","Море")}<br>{index.get(owners.get(t["province_id"]),{}).get("name","")}<br>{", ".join(t["resources"])} · {"дорога" if t["road"] else "без дороги"}' for t in cells],hoverinfo='text'))
    for state_border in (False,True):
        xs=[]; ys=[]
        for (x,y),t in by_xy.items():
            for dx,dy in ((1,0),(0,1)):
                n=by_xy.get((x+dx,y+dy))
                if not n or not t['passable'] or not n['passable'] or t['province_id']==n['province_id']: continue
                is_state=owners.get(t['province_id'])!=owners.get(n['province_id'])
                if is_state!=state_border: continue
                if dx: xs.extend([x+.5,x+.5,None]); ys.extend([y-.5,y+.5,None])
                else: xs.extend([x-.5,x+.5,None]); ys.extend([y+.5,y+.5,None])
        fig.add_trace(go.Scatter(x=xs,y=ys,mode='lines',name='Границы государств' if state_border else 'Границы провинций',
                                line=dict(color='#19202d' if state_border else '#eef0d9',width=3 if state_border else 1),hoverinfo='skip'))
    roads=[t for t in tiles if t['road']]
    xs=[];ys=[]
    for t in roads:
        for xy in ((t['x']+1,t['y']),(t['x'],t['y']+1)):
            n=by_xy.get(xy)
            if n and n['road']: xs.extend([t['x'],n['x'],None]);ys.extend([t['y'],n['y'],None])
    fig.add_trace(go.Scatter(x=xs,y=ys,mode='lines',line=dict(color='#f4dfb1',width=2),name='Дороги',hoverinfo='skip'))
    capitals={e['properties'].get('capital_id') for e in index.values() if e['kind']=='State'}
    for capital in (False,True):
        cities=[e for e in index.values() if e['kind']=='City' and (e['id'] in capitals)==capital and e['properties'].get('tile_id') in by_id]
        fig.add_trace(go.Scatter(x=[by_id[e['properties']['tile_id']]['x'] for e in cities],y=[by_id[e['properties']['tile_id']]['y'] for e in cities],
            mode='markers+text',text=[e['name'] for e in cities],textposition='top center',textfont=dict(size=10,color='#fff'),
            customdata=[e['id'] for e in cities],hovertext=[e['name'] for e in cities],hoverinfo='text',
            name='Столицы государств' if capital else 'Города',marker=dict(symbol='star' if capital else 'diamond',size=15 if capital else 10,color='#fff1a8',line=dict(width=1,color='#222'))))
    if route_path:
        fig.add_trace(go.Scatter(x=[by_id[i]['x'] for i in route_path],y=[by_id[i]['y'] for i in route_path],mode='lines',line=dict(color='#ff4b4b',width=4),name='Маршрут'))
    fig.update_layout(height=680,dragmode='pan',clickmode='event+select',uirevision='map-v2',
        margin=dict(l=5,r=5,t=5,b=5),xaxis=dict(visible=False),yaxis=dict(visible=False,scaleanchor='x',autorange='reversed'),
        legend=dict(orientation='h',font=dict(size=10)),plot_bgcolor='#152638')
    return fig
