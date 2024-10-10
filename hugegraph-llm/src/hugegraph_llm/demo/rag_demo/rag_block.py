# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

# pylint: disable=E1101

import json
import os
from typing import Tuple, List, Literal, Optional

from datasets import Dataset
import gradio as gr
from gradio.utils import NamedString
import pandas as pd
from ragas import evaluate

from hugegraph_llm.config import resource_path, prompt
from hugegraph_llm.operators.graph_rag_task import RAGPipeline
from hugegraph_llm.utils.log import log
from hugegraph_llm.utils.ragas_utils import RAGAS_METRICS_DICT


def rag_answer(
        text: str,
        raw_answer: bool,
        vector_only_answer: bool,
        graph_only_answer: bool,
        graph_vector_answer: bool,
        graph_ratio: float,
        rerank_method: Literal["bleu", "reranker"],
        near_neighbor_first: bool,
        custom_related_information: str,
        answer_prompt: str,
) -> Tuple:
    """
    Generate an answer using the RAG (Retrieval-Augmented Generation) pipeline.
    1. Initialize the RAGPipeline.
    2. Select vector search or graph search based on parameters.
    3. Merge, deduplicate, and rerank the results.
    4. Synthesize the final answer.
    5. Run the pipeline and return the results.
    """
    should_update_prompt = prompt.default_question != text or prompt.answer_prompt != answer_prompt
    if should_update_prompt or prompt.custom_rerank_info != custom_related_information:
        prompt.custom_rerank_info = custom_related_information
        prompt.default_question = text
        prompt.answer_prompt = answer_prompt
        prompt.update_yaml_file()

    vector_search = vector_only_answer or graph_vector_answer
    graph_search = graph_only_answer or graph_vector_answer
    if raw_answer is False and not vector_search and not graph_search:
        gr.Warning("Please select at least one generate mode.")
        return "", "", "", ""

    rag = RAGPipeline()
    if vector_search:
        rag.query_vector_index()
    if graph_search:
        rag.extract_keywords().keywords_to_vid().query_graphdb()
    # TODO: add more user-defined search strategies
    rag.merge_dedup_rerank(graph_ratio, rerank_method, near_neighbor_first, custom_related_information)
    rag.synthesize_answer(answer_prompt)

    try:
        context = rag.run(
            verbose=True,
            query=text,
            raw_answer=raw_answer,
            vector_only_answer=vector_only_answer,
            graph_only_answer=graph_only_answer,
            graph_vector_answer=graph_vector_answer,
        )
        if context.get("switch_to_bleu"):
            gr.Warning("Online reranker fails, automatically switches to local bleu rerank.")
        return (
            context.get("raw_answer_result", ""),
            context.get("vector_only_answer_result", ""),
            context.get("graph_only_answer_result", ""),
            context.get("graph_vector_answer_result", ""),
            {
                "vector_contexts": context.get("vector_contexts"),
                "graph_contexts": context.get("graph_contexts"),
                "graph_vector_contexts": context.get("graph_vector_contexts"),
            },
        )
    except ValueError as e:
        log.critical(e)
        raise gr.Error(str(e))
    except Exception as e:
        log.critical(e)
        raise gr.Error(f"An unexpected error occurred: {str(e)}")


def create_rag_block():
    # pylint: disable=R0915 (too-many-statements)
    gr.Markdown("""## 2. RAG with HugeGraph""")
    with gr.Row():
        with gr.Column(scale=2):
            inp = gr.Textbox(value=prompt.default_question, label="Question", show_copy_button=True, lines=2)
            raw_out = gr.Textbox(label="Basic LLM Answer", show_copy_button=True)
            vector_only_out = gr.Textbox(label="Vector-only Answer", show_copy_button=True)
            graph_only_out = gr.Textbox(label="Graph-only Answer", show_copy_button=True)
            graph_vector_out = gr.Textbox(label="Graph-Vector Answer", show_copy_button=True)
            from hugegraph_llm.operators.llm_op.answer_synthesize import DEFAULT_ANSWER_TEMPLATE

            answer_prompt_input = gr.Textbox(
                value=DEFAULT_ANSWER_TEMPLATE, label="Custom Prompt", show_copy_button=True, lines=7
            )
        with gr.Column(scale=1):
            with gr.Row():
                raw_radio = gr.Radio(choices=[True, False], value=True, label="Basic LLM Answer")
                vector_only_radio = gr.Radio(choices=[True, False], value=False, label="Vector-only Answer")
            with gr.Row():
                graph_only_radio = gr.Radio(choices=[True, False], value=False, label="Graph-only Answer")
                graph_vector_radio = gr.Radio(choices=[True, False], value=False, label="Graph-Vector Answer")

            def toggle_slider(enable):
                return gr.update(interactive=enable)

            with gr.Column():
                with gr.Row():
                    online_rerank = os.getenv("reranker_type")
                    rerank_method = gr.Dropdown(
                        choices=["bleu", ("rerank (online)", "reranker")] if online_rerank else ["bleu"],
                        value="reranker" if online_rerank else "bleu",
                        label="Rerank method",
                    )
                    graph_ratio = gr.Slider(0, 1, 0.5, label="Graph Ratio", step=0.1, interactive=False)

                graph_vector_radio.change(toggle_slider, inputs=graph_vector_radio, outputs=graph_ratio)  # pylint: disable=no-member
                near_neighbor_first = gr.Checkbox(
                    value=False,
                    label="Near neighbor first(Optional)",
                    info="One-depth neighbors > two-depth neighbors",
                )
                custom_related_information = gr.Text(
                    prompt.custom_rerank_info,
                    label="Custom related information(Optional)",
                    info=(
                        "Used for rerank, can increase the weight of knowledge related to it, such as `law`. "
                        "Multiple values can be separated by commas."
                    ),
                )
                btn = gr.Button("Answer Question", variant="primary")

    btn.click(  # pylint: disable=no-member
        fn=rag_answer,
        inputs=[
            inp,
            raw_radio,
            vector_only_radio,
            graph_only_radio,
            graph_vector_radio,
            graph_ratio,
            rerank_method,
            near_neighbor_first,
            custom_related_information,
            answer_prompt_input,
        ],
        outputs=[raw_out, vector_only_out, graph_only_out, graph_vector_out],
    )

    gr.Markdown("""## 3. User Functions (Back-testing)
    > 1. Download the template file & fill in the questions you want to test.
    > 2. Upload the file & click the button to generate answers. (Preview shows the first 40 lines)
    > 3. The answer options are the same as the above RAG/Q&A frame 
    """
    )

    # TODO: Replace string with python constant
    tests_df_headers = [
        "Question",
        "Graph-only Answer",
        "Graph-Vector Answer",
        "Vector-only Answer",
        "Basic LLM Answer",
        "Expected Answer",
    ]
    rag_answer_header_dict = {
        "Vector-only Answer": "Vector Contexts",
        "Graph-only Answer": "Graph Contexts",
        "Graph-Vector Answer": "Graph-Vector Contexts",
    }

    answers_path = os.path.join(resource_path, "demo", "questions_answers.xlsx")
    questions_path = os.path.join(resource_path, "demo", "questions.xlsx")
    questions_template_path = os.path.join(resource_path, "demo", "questions_template.xlsx")

    ragas_metrics_list = list(RAGAS_METRICS_DICT.keys())

    def read_file_to_excel(file: NamedString, line_count: Optional[int] = None):
        if os.path.exists(answers_path):
            os.remove(answers_path)
        df = pd.DataFrame()
        if not file:
            return pd.DataFrame(), 1
        if file.name.endswith(".xlsx"):
            df = pd.read_excel(file.name, nrows=line_count) if file else pd.DataFrame()
        elif file.name.endswith(".csv"):
            df = pd.read_csv(file.name, nrows=line_count) if file else pd.DataFrame()
        else:
            raise gr.Error("Only support .xlsx and .csv files.")
        df.to_excel(questions_path, index=False)
        # truncate the dataframe if it's too long
        if len(df) > 40:
            return df.head(40), 40
        if len(df) == 0:
            gr.Warning("No data in the file.")
        return df, len(df)

    def change_showing_excel(line_count):
        if os.path.exists(answers_path):
            df = pd.read_excel(answers_path, nrows=line_count)
        elif os.path.exists(questions_path):
            df = pd.read_excel(questions_path, nrows=line_count)
        else:
            df = pd.read_excel(questions_template_path, nrows=line_count)
        return df

    def several_rag_answer(
        is_raw_answer: bool,
        is_vector_only_answer: bool,
        is_graph_only_answer: bool,
        is_graph_vector_answer: bool,
        graph_ratio: float,
        rerank_method: Literal["bleu", "reranker"],
        near_neighbor_first: bool,
        custom_related_information: str,
        answer_prompt: str,
        progress=gr.Progress(track_tqdm=True),
        answer_max_line_count: int = 1,
    ):
        df = pd.read_excel(questions_path, dtype=str)
        total_rows = len(df)
        for index, row in df.iterrows():
            question = row.iloc[0]
            llm_answer, vector_only_answer, graph_only_answer, graph_vector_answer, contexts = rag_answer(
                question,
                is_raw_answer,
                is_vector_only_answer,
                is_graph_only_answer,
                is_graph_vector_answer,
                graph_ratio,
                rerank_method,
                near_neighbor_first,
                custom_related_information,
                answer_prompt,
            )
            df.at[index, "Basic LLM Answer"] = llm_answer if llm_answer else None
            df.at[index, "Vector-only Answer"] = vector_only_answer if vector_only_answer else None
            df.at[index, "Graph-only Answer"] = graph_only_answer if graph_only_answer else None
            df.at[index, "Graph-Vector Answer"] = graph_vector_answer if graph_vector_answer else None
            if "Vector Contexts" not in df.columns:
                df["Vector Contexts"] = None
                df["Graph Contexts"] = None
                df["Graph-Vector Contexts"] = None
            df.at[index, "Vector Contexts"] = contexts.get("vector_contexts")
            df.at[index, "Graph Contexts"] = contexts.get("graph_contexts")
            df.at[index, "Graph-Vector Contexts"] = contexts.get("graph_vector_contexts")
            progress((index + 1, total_rows))

        df = df.dropna(axis=1, how="all")
        df_to_show = df[[col for col in tests_df_headers if col in df.columns]]
        for rag_context_header in rag_answer_header_dict.values():
            if rag_context_header in df.columns:
                df[rag_context_header] = df[rag_context_header].apply(lambda x: json.dumps(x, ensure_ascii=False))
        df.to_excel(answers_path, index=False)
        return df_to_show.head(answer_max_line_count), answers_path

    with gr.Row():
        with gr.Column():
            questions_file = gr.File(file_types=[".xlsx", ".csv"], label="Questions File (.xlsx & .csv)")
        with gr.Column():
            test_template_file = os.path.join(resource_path, "demo", "questions_template.xlsx")
            gr.File(value=test_template_file, label="Download Template File")
            answer_max_line_count = gr.Number(1, label="Max Lines To Show", minimum=1, maximum=40)
            answers_btn = gr.Button("Generate Answer (Batch)", variant="primary")
    # TODO: Set individual progress bars for dataframe
    qa_dataframe = gr.DataFrame(label="Questions & Answers (Preview)", headers=tests_df_headers)
    answers_btn.click(
        several_rag_answer,
        inputs=[
            raw_radio,
            vector_only_radio,
            graph_only_radio,
            graph_vector_radio,
            graph_ratio,
            rerank_method,
            near_neighbor_first,
            custom_related_information,
            answer_prompt_input,
            answer_max_line_count,
        ],
        outputs=[qa_dataframe, gr.File(label="Download Answered File", min_width=40)],
    )
    questions_file.change(read_file_to_excel, questions_file, [qa_dataframe, answer_max_line_count])
    answer_max_line_count.change(change_showing_excel, answer_max_line_count, qa_dataframe)

    def evaluate_rag(metrics: List[str], num: int):
        answers_df = pd.read_excel(answers_path)
        answers_df = answers_df.head(num)
        if not any(answers_df.columns.isin(rag_answer_header_dict)):
            raise gr.Error("No RAG answers found in the answer file.")
        rag_answers = [answer for answer in rag_answer_header_dict if answer in answers_df.columns]
        df = pd.DataFrame()

        for answer in rag_answers:
            context_header = rag_answer_header_dict[answer]
            answers_df[context_header] = answers_df[context_header].apply(json.loads)
            rag_data = {
                "question": answers_df["Question"].to_list(),
                "answer": answers_df[answer].to_list(),
                "contexts": answers_df[rag_answer_header_dict[answer]].to_list(),
                "ground_truth": answers_df["Expected Answer"].to_list(),
            }
            dataset = Dataset.from_dict(rag_data)
            score = evaluate(dataset, metrics=[RAGAS_METRICS_DICT[metric] for metric in metrics])
            print(score.scores.to_pandas())
            df = pd.concat([df, score.scores.to_pandas()])
        df.insert(0, 'method', rag_answers)
        return df

    with gr.Row():
        with gr.Column():
            ragas_metrics = gr.Dropdown(
                choices=ragas_metrics_list,
                value=ragas_metrics_list[:4],
                multiselect=True,
                label="Metrics",
                info="Several evaluation metrics from `ragas`, please refer to https://docs.ragas.io/en/stable/concepts/metrics/index.html",
            )
        with gr.Column():
            dataset_nums = gr.Number(1, label="Dataset Numbers", minimum=1, maximum=1)
            ragas_btn = gr.Button("Evaluate RAG", variant="primary")
    ragas_btn.click(
        evaluate_rag,
        inputs=[ragas_metrics, dataset_nums],
        outputs=[gr.DataFrame(label="RAG Evaluation Results", headers=ragas_metrics_list)],
    )
