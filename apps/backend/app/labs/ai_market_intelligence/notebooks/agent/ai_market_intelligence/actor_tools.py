"""Executable source tools; MCP start/status/dataset remain private transport operations."""
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field

from .actor_profiles import PROFILES
from .connectors import compile_call
from .contracts import Output, Short


class ActorQueryInput(Output):
    query: Short
    max_items: Annotated[int,Field(ge=1,le=50)]


class ActorXInput(ActorQueryInput):
    sort: Literal['Latest'] = 'Latest'


class ActorGoogleInput(ActorQueryInput):
    max_items: Literal[10]
    search_mode: Literal['research','navigation'] = 'research'


class ActorPageInput(Output):
    urls: Annotated[list[Short],Field(min_length=1,max_length=3)]
    max_items: Annotated[int,Field(ge=1,le=50)]


class ActorHistoryInput(Output):
    tickers: Annotated[list[Short],Field(min_length=1,max_length=3)]
    start_date: Annotated[str,Field(pattern=r'^\d{4}-\d{2}-\d{2}$')]
    end_date: Annotated[str,Field(pattern=r'^\d{4}-\d{2}-\d{2}$')]
    max_items: Annotated[int,Field(ge=1,le=50)]


class ActorIndicatorInput(Output):
    countries: Annotated[list[Short],Field(min_length=1,max_length=3)]
    indicator_codes: Annotated[list[Short],Field(min_length=1,max_length=3)]
    year_from: Annotated[int,Field(ge=1960,le=2100)]
    year_to: Annotated[int,Field(ge=1960,le=2100)]
    max_items: Annotated[int,Field(ge=1,le=50)]


@dataclass(frozen=True)
class ActorTool:
    name: str
    source: str
    arguments_schema: type
    result_kind: str

    @property
    def maximum_items(self):
        return 3 if self.name=='website' else PROFILES[self.name].get('max_items',50)

    def specification(self):
        schema=self.arguments_schema.model_json_schema()
        schema['properties']['max_items']['maximum']=self.maximum_items
        return {'name':self.name,'source':self.source,'actor':PROFILES[self.name]['actor'],
                'arguments_schema':schema,'result_kind':self.result_kind}

    def compile(self,arguments,window,**authority):
        values=self.arguments_schema.model_validate(arguments).model_dump(mode='json')
        if values['max_items']>self.maximum_items:
            raise ValueError('Tool arguments exceed this source result limit')
        args={'query':'','urls':[],'sort':values.pop('sort','Latest'),'max_items':values.pop('max_items')}
        if self.name in {'yahoo_history','world_bank'}:
            args['financial']=values
        else:
            args.update(values)
        return compile_call(self.name,args,window,None,**authority)

    async def ainvoke(self,arguments,*,window,executor,**authority):
        return await executor(self.compile(arguments,window,**authority))


ACTOR_TOOLS={tool.name:tool for tool in (
    ActorTool('web_search_serp','web',ActorGoogleInput,'search_candidates'),
    ActorTool('website','web',ActorPageInput,'page_text'),
    ActorTool('x_search','x',ActorXInput,'public_posts'),
    ActorTool('youtube_search','youtube',ActorQueryInput,'video_titles_descriptions'),
    ActorTool('facebook_search','facebook',ActorQueryInput,'keyword_posts'),
    ActorTool('instagram_search','instagram',ActorQueryInput,'popular_reel_captions'),
    ActorTool('facebook_posts','facebook',ActorPageInput,'selected_destination_posts'),
    ActorTool('instagram_posts','instagram',ActorPageInput,'selected_destination_posts'),
    ActorTool('yahoo_history','market_data',ActorHistoryInput,'daily_market_series'),
    ActorTool('world_bank','world_bank',ActorIndicatorInput,'annual_indicator_series'))}


def actor_tool(name):
    if name not in ACTOR_TOOLS:
        raise ValueError('The requested Actor tool is not in the reviewed v2 catalogue')
    return ACTOR_TOOLS[name]


def actor_tool_specifications(sources):
    admitted=set(sources)
    if 'financial_document' in admitted:
        admitted.add('web')
    return [tool.specification() for tool in ACTOR_TOOLS.values() if tool.source in admitted]
