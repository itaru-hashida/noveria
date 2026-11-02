import html
import networkx as nx
import plotly.graph_objects as go

COLORS = dict(State='#e5b85c', Province='#5aa9b5', City='#7bc99c', Character='#c18ae0', SocialGroup='#f08c76', Army='#91a0b8', Institution='#e5dca0')


def graph_figure(data):
    graph = nx.MultiDiGraph()
    for node in data['nodes']:
        graph.add_node(node['id'])
    for edge in data['edges']:
        graph.add_edge(edge['source'], edge['target'])
    pos = nx.spring_layout(graph, seed=42)
    fig = go.Figure()
    for kind in sorted({e['kind'] for e in data['edges']}):
        x, y, lx, ly, hover = [], [], [], [], []
        for edge in data['edges']:
            if edge['kind'] != kind:
                continue
            a, b = pos[edge['source']], pos[edge['target']]
            x.extend([a[0], b[0], None]); y.extend([a[1], b[1], None])
            lx.append((a[0]+b[0])/2); ly.append((a[1]+b[1])/2)
            hover.append(f'{edge["source"]} → {edge["target"]}: {kind}')
            fig.add_annotation(x=b[0]*.85+a[0]*.15, y=b[1]*.85+a[1]*.15, ax=a[0], ay=a[1],
                               xref='x', yref='y', axref='x', ayref='y', showarrow=True,
                               arrowhead=2, arrowwidth=1, arrowcolor='#8795a5', text='')
        fig.add_trace(go.Scatter(x=x, y=y, mode='lines', name=kind, line=dict(width=1,color='#8795a5'), hoverinfo='skip'))
        fig.add_trace(go.Scatter(x=lx, y=ly, mode='markers+text' if len(data['edges'])<45 else 'markers',
                                 text=[kind]*len(lx), hovertext=hover, hoverinfo='text',
                                 marker=dict(size=5,color='#8795a5'), textfont=dict(size=9), showlegend=False))
    for kind, color in COLORS.items():
        nodes = [n for n in data['nodes'] if n['kind']==kind]
        if not nodes:
            continue
        fig.add_trace(go.Scatter(x=[pos[n['id']][0] for n in nodes], y=[pos[n['id']][1] for n in nodes],
                                 mode='markers+text', text=[n['name'] for n in nodes], textposition='top center',
                                 name=kind, customdata=[n['id'] for n in nodes],
                                 hovertext=[html.escape(f'{n["name"]} | {kind} | {str(n["properties"])[:250]}') for n in nodes],
                                 hoverinfo='text', marker=dict(size=16,color=color,line=dict(width=1,color='white'))))
    fig.update_layout(height=650, dragmode='pan', margin=dict(l=10,r=10,t=10,b=10),
                       xaxis=dict(visible=False), yaxis=dict(visible=False), legend=dict(orientation='h'))
    return fig
