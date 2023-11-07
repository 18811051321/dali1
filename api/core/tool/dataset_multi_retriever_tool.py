import json
import threading
from typing import Type, Optional, List

from flask import current_app, Flask
from flask_login import current_user
from langchain.tools import BaseTool
from pydantic import Field, BaseModel

from core.callback_handler.index_tool_callback_handler import DatasetIndexToolCallbackHandler
from core.conversation_message_task import ConversationMessageTask
from core.embedding.cached_embedding import CacheEmbedding
from core.index.keyword_table_index.keyword_table_index import KeywordTableIndex, KeywordTableConfig
from core.index.vector_index.vector_index import VectorIndex
from core.model_providers.error import LLMBadRequestError, ProviderTokenNotInitError
from core.model_providers.model_factory import ModelFactory
from extensions.ext_database import db
from models.dataset import Dataset, DocumentSegment, Document
from services.retrival_service import RetrivalService

default_retrival_model = {
    'search_method': 'semantic_search',
    'reranking_enable': False,
    'reranking_model': {
        'reranking_provider_name': '',
        'reranking_model_name': ''
    },
    'top_k': 2,
    'score_threshold_enable': False
}


class DatasetMultiRetrieverToolInput(BaseModel):
    query: str = Field(..., description="dataset multi retriever and rerank")


class DatasetMultiRetrieverTool(BaseTool):
    """Tool for querying multi dataset."""
    name: str = "multi_datasets"
    args_schema: Type[BaseModel] = DatasetMultiRetrieverToolInput
    description: str = "dataset multi retriever and rerank. "
    tenant_id: str
    dataset_ids: List[str]
    top_k: int = 2
    score_threshold: Optional[float] = None
    reranking_provider_name: str
    reranking_model_name: str
    conversation_message_task: ConversationMessageTask
    return_resource: bool
    retriever_from: str

    @classmethod
    def from_dataset(cls, dataset_ids: List[str], **kwargs):
        return cls(
            name=f'multi_datasets-{current_user.current_tenant_id}',
            tenant_id=current_user.current_tenant_id,
            dataset_id=dataset_ids,
            **kwargs
        )

    def _run(self, query: str) -> str:
        threads = []
        all_documents = []
        for dataset_id in self.dataset_ids:
            retrival_thread = threading.Thread(target=self._retriever, kwargs={
                'flask_app': current_app._get_current_object(),
                'dataset_id': dataset_id,
                'query': query,
                'all_documents': all_documents
            })
            threads.append(retrival_thread)
        # do rerank for searched documents
        rerank = ModelFactory.get_reranking_model(
            tenant_id=self.tenant_id,
            model_provider_name=self.reranking_provider_name,
            model_name=self.reranking_model_name
        )
        all_documents = rerank.rerank(query, all_documents, self.score_threshold, self.top_k)

        hit_callback = DatasetIndexToolCallbackHandler(self.conversation_message_task)
        hit_callback.on_tool_end(all_documents)

        document_context_list = []
        index_node_ids = [document.metadata['doc_id'] for document in all_documents]
        segments = DocumentSegment.query.filter(
            DocumentSegment.completed_at.isnot(None),
            DocumentSegment.status == 'completed',
            DocumentSegment.enabled == True,
            DocumentSegment.index_node_id.in_(index_node_ids)
        ).all()

        if segments:
            index_node_id_to_position = {id: position for position, id in enumerate(index_node_ids)}
            sorted_segments = sorted(segments,
                                     key=lambda segment: index_node_id_to_position.get(segment.index_node_id,
                                                                                       float('inf')))
            for segment in sorted_segments:
                if segment.answer:
                    document_context_list.append(f'question:{segment.content} answer:{segment.answer}')
                else:
                    document_context_list.append(segment.content)
            if self.return_resource:
                context_list = []
                resource_number = 1
                for segment in sorted_segments:
                    dataset = Dataset.query.filter_by(
                        id=segment.dataset_id
                    ).first()
                    document = Document.query.filter(Document.id == segment.document_id,
                                                     Document.enabled == True,
                                                     Document.archived == False,
                                                     ).first()
                    if dataset and document:
                        source = {
                            'position': resource_number,
                            'dataset_id': dataset.id,
                            'dataset_name': dataset.name,
                            'document_id': document.id,
                            'document_name': document.name,
                            'data_source_type': document.data_source_type,
                            'segment_id': segment.id,
                            'retriever_from': self.retriever_from
                        }
                        if self.retriever_from == 'dev':
                            source['hit_count'] = segment.hit_count
                            source['word_count'] = segment.word_count
                            source['segment_position'] = segment.position
                            source['index_node_hash'] = segment.index_node_hash
                        if segment.answer:
                            source['content'] = f'question:{segment.content} \nanswer:{segment.answer}'
                        else:
                            source['content'] = segment.content
                        context_list.append(source)
                    resource_number += 1
                hit_callback.return_retriever_resource_info(context_list)

            return str("\n".join(document_context_list))

    async def _arun(self, tool_input: str) -> str:
        raise NotImplementedError()

    def _retriever(self, flask_app: Flask, dataset_id: str, query: str, all_documents: List):
        with flask_app.app_context():
            dataset = db.session.query(Dataset).filter(
                Dataset.tenant_id == self.tenant_id,
                Dataset.id == dataset_id
            ).first()

            if not dataset:
                return []
            # get retrival model , if the model is not setting , using default
            retrival_model = json.loads(dataset.retrieval_model) if dataset.retrieval_model else default_retrival_model

            if dataset.indexing_technique == "economy":
                # use keyword table query
                kw_table_index = KeywordTableIndex(
                    dataset=dataset,
                    config=KeywordTableConfig(
                        max_keywords_per_chunk=5
                    )
                )

                documents = kw_table_index.search(query, search_kwargs={'k': self.top_k})
                if documents:
                    all_documents.extend(documents)
            else:

                try:
                    embedding_model = ModelFactory.get_embedding_model(
                        tenant_id=dataset.tenant_id,
                        model_provider_name=dataset.embedding_model_provider,
                        model_name=dataset.embedding_model
                    )
                except LLMBadRequestError:
                    return []
                except ProviderTokenNotInitError:
                    return []

                embeddings = CacheEmbedding(embedding_model)

                documents = []
                threads = []
                if self.top_k > 0:
                    # retrival source with semantic
                    if retrival_model['search_method'] == 'semantic_search' or retrival_model[
                        'search_method'] == 'hybrid_search':
                        embedding_thread = threading.Thread(target=RetrivalService.embedding_search, kwargs={
                            'flask_app': current_app._get_current_object(),
                            'dataset': dataset,
                            'query': query,
                            'top_k': self.top_k,
                            'score_threshold': self.score_threshold,
                            'reranking_model': None,
                            'all_documents': documents,
                            'search_method': 'hybrid_search',
                            'embeddings': embeddings
                        })
                        threads.append(embedding_thread)
                        embedding_thread.start()

                    # retrival source with full text
                    if retrival_model['search_method'] == 'full_text-search' or retrival_model[
                        'search_method'] == 'hybrid_search':
                        full_text_index_thread = threading.Thread(target=RetrivalService.full_text_index_search,
                                                                  kwargs={
                                                                      'flask_app': current_app._get_current_object(),
                                                                      'dataset': dataset,
                                                                      'query': query,
                                                                      'search_method': 'hybrid_search',
                                                                      'embeddings': embeddings,
                                                                      'score_threshold': retrival_model[
                                                                          'score_threshold'] if retrival_model[
                                                                          'score_threshold_enable'] else None,
                                                                      'top_k': self.top_k,
                                                                      'reranking_model': retrival_model[
                                                                          'reranking_model'] if retrival_model[
                                                                          'reranking_enable'] else None,
                                                                      'all_documents': documents
                                                                  })
                        threads.append(full_text_index_thread)
                        full_text_index_thread.start()

                    for thread in threads:
                        thread.join()

                    all_documents.extend(documents)
