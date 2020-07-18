import os
import numpy as np
import torch

from tqdm import tqdm
from transformers import BartTokenizer, AlbertTokenizer, BertTokenizer
from transformers import BartConfig, AlbertConfig, BertConfig
from transformers import AdamW, get_linear_schedule_with_warmup

from Data import QAData, AmbigQAData
from PassageData import PassageData
from models.span_predictor import SpanPredictor, AlbertSpanPredictor
from models.seq2seq import MyBart
from models.biencoder import MyBiEncoder
from ambigqa_evaluate_script import get_exact_match
from IPython import embed

def run(args, logger):

    args.dpr = args.task=="dpr"
    args.is_seq2seq = 'bart' in args.bert_name
    if 'bart' in args.bert_name:
        tokenizer = BartTokenizer.from_pretrained(args.bert_name)
        tokenizer.add_tokens(["<SEP>"])
        Model = MyBart
        Config = BartConfig
        args.append_another_bos = True
    elif 'albert' in args.bert_name:
        tokenizer = AlbertTokenizer.from_pretrained(args.bert_name)
        Model = AlbertSpanPredictor
        Config = AlbertConfig
    elif 'bert' in args.bert_name:
        tokenizer = BertTokenizer.from_pretrained(args.bert_name)
        Model = MyBiEncoder if args.dpr else SpanPredictor
        Config = BertConfig
    else:
        raise NotImplementedError()

    if args.dpr:
        tokenizer = BertTokenizer.from_pretrained("bert-base-uncased")
        Model = MyBiEncoder
        args.checkpoint = os.path.join(args.dpr_data_dir, "checkpoint/retriever/multiset/bert-base-encoder.cp")
        assert not args.do_train, "Training DPR is not supported yet"

    passages = PassageData(logger, args, tokenizer)

    def _getQAData():
        if args.ambigqa:
            return AmbigQAData #if args.is_seq2seq else AmbigQAData
        else:
            return QAData #if args.is_seq2seq else QAData

    def _load_from_checkpoint(checkpoint):
        def convert_to_single_gpu(state_dict):
            def _convert(key):
                if key.startswith('module.'):
                    return key[7:]
                return key
            return {_convert(key):value for key, value in state_dict.items()}
        state_dict = convert_to_single_gpu(torch.load(checkpoint))
        model = Model(Config.from_pretrained(args.bert_name))
        if "bart" in args.bert_name:
            model.resize_token_embeddings(len(tokenizer))
        return model.from_pretrained(None, config=model.config, state_dict=state_dict)
        '''
        if "bart" in args.bert_name:
            def _convert(key, value):
                if key=="final_logits_bias":
                    assert tuple(value.shape)==(1, 50266)
                    return value[:,:50265]
                if key in ["model.shared.weight", "model.encoder.embed_tokens.weight", "model.decoder.embed_tokens.weight"]:
                    assert tuple(value.shape)==(50266, 1024)
                    return value[:50265, :]
                return value
            state_dict = {key:_convert(key, value) for key, value in state_dict.items()}
        return Model.from_pretrained(args.bert_name, state_dict=state_dict)
        '''

    if args.do_train and args.skip_inference:
        dev_data = None
    else:
        dev_data = _getQAData()(logger, args, args.predict_file, False, passages)
        dev_data.load_dataset(tokenizer)
        dev_data.load_dataloader()

    if args.do_train:
        train_data = _getQAData()(logger, args, args.train_file, True, passages)
        train_data.load_dataset(tokenizer)
        train_data.load_dataloader()

        if args.checkpoint is not None:
            model = _load_from_checkpoint(args.checkpoint)
        else:
            model = Model.from_pretrained(args.bert_name)
        if "bart" in args.bert_name:
            model.resize_token_embeddings(len(tokenizer))
        if args.n_gpu>1:
            model = torch.nn.DataParallel(model)
        model.to(torch.device("cuda"))

        no_decay = ['bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)], 'weight_decay': args.weight_decay},
            {'params': [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
            ]
        optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
        scheduler =  get_linear_schedule_with_warmup(optimizer,
                                        num_warmup_steps=args.warmup_steps,
                                        num_training_steps=100000)
        train(args, logger, model, train_data, dev_data, optimizer, scheduler)

    if args.do_predict:
        checkpoint = os.path.join(args.output_dir, 'best-model.pt') if args.checkpoint is None else args.checkpoint
        model = _load_from_checkpoint(checkpoint)
        logger.info("Loading checkpoint from {}".format(checkpoint))
        if args.n_gpu>1 and 'bert' in args.bert_name:
            model = torch.nn.DataParallel(model)
        model.to(torch.device("cuda"))
        model.eval()
        ems = inference(model, dev_data, save_predictions=True)
        logger.info("%s on test data = %.2f" % (dev_data.metric, np.mean(ems)*100))

def train(args, logger, model, train_data, dev_data, optimizer, scheduler):
    model.train()
    global_step = 0
    train_losses = []
    best_accuracy = -1
    stop_training=False

    logger.info("Start training!")
    for epoch in range(int(args.num_train_epochs)):
        for batch in train_data.dataloader:
            global_step += 1
            batch = [b.to(torch.device("cuda")) for b in batch]
            if args.is_seq2seq:
                loss = model(input_ids=batch[0], attention_mask=batch[1],
                             decoder_input_ids=batch[2], decoder_attention_mask=batch[3],
                             is_training=True)
            else:
                loss = model(input_ids=batch[0], attention_mask=batch[1], token_type_ids=batch[2],
                             start_positions=batch[3], end_positions=batch[4], answer_mask=batch[5],
                             is_training=True)
            if args.n_gpu > 1:
                loss = loss.mean() # mean() to average on multi-gpu.
            if torch.isnan(loss).data:
                logger.info("Stop training because loss=%s" % (loss.data))
                stop_training=True
                break
            train_losses.append(loss.detach().cpu())
            loss.backward()

            if global_step % args.gradient_accumulation_steps == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
                optimizer.step()    # We have accumulated enought gradients
                scheduler.step()
                model.zero_grad()

            if global_step % args.eval_period == 0:
                if args.skip_inference:
                    logger.info("Step %d (epoch %d) Train loss %.2f" % (
                            global_step,
                            epoch,
                            np.mean(train_losses)))
                    train_losses = []
                    model_state_dict = {k:v.cpu() for (k, v) in model.state_dict().items()}
                    torch.save(model_state_dict, os.path.join(args.output_dir,
                                                              "best-model-{}.pt".format(str(global_step).zfill(6))))
                else:
                    model.eval()
                    curr_em = inference(model, dev_data)
                    logger.info("Step %d Train loss %.2f %s %.2f%% on epoch=%d" % (
                            global_step,
                            np.mean(train_losses),
                            dev_data.metric,
                            curr_em*100,
                            epoch))
                    train_losses = []
                    if best_accuracy < curr_em:
                        model_state_dict = {k:v.cpu() for (k, v) in model.state_dict().items()}
                        torch.save(model_state_dict, os.path.join(args.output_dir, "best-model.pt"))
                        logger.info("Saving model with best %s: %.2f%% -> %.2f%% on epoch=%d, global_step=%d" % \
                                (dev_data.metric, best_accuracy*100.0, curr_em*100.0, epoch, global_step))
                        best_accuracy = curr_em
                        wait_step = 0
                        stop_training = False
                    else:
                        wait_step += 1
                        if wait_step >= args.wait_step:
                            stop_training = True
                            break
                    model.train()
        if stop_training:
            break

def inference(model, dev_data, save_predictions=False):
    if dev_data.args.dpr:
        return inference_dpr(model, dev_data, save_predictions)
    if "bart" in dev_data.args.bert_name:
        return inference_seq2seq(model if dev_data.args.n_gpu==1 or dev_data.args.do_predict else model.module, dev_data, save_predictions)
    return inference_span_predictor(model, dev_data, save_predictions)

def inference_dpr(model, dev_data, save_predictions):

    def _inference(dataloader, is_passages):
        if dev_data.args.n_gpu>1:
            curr_model = model.module.ctx_model if is_passages else model.module.question_model
            curr_model = torch.nn.DataParallel(curr_model)
        else:
            curr_model = model.ctx_model if is_passages else model.question_model
        vectors = []
        for i, batch in tqdm(enumerate(dataloader)):
            with torch.no_grad():
                batch = [b.to(torch.device("cuda")) for b in batch]
                outputs = curr_model(input_ids=batch[0], attention_mask=batch[1])[0][:,0,:]
                vectors.append(outputs.detach().cpu().numpy())
        return np.concatenate(vectors, axis=0)

    checkpoint = dev_data.args.checkpoint
    assert checkpoint is not None
    import faiss
    index_path = checkpoint[:checkpoint.index(".")] + ".IndexFlatIP"
    if os.path.exists(index_path):
        index = faiss.read_index(index_path)
        #with open(index_path, "rb") as f:
        #    reader = faiss.PyCallbackIOReader(lambda x: 1234) #lambda size: os.read(f, size))
        #    index = faiss.read_index(reader)
    else:
        checkpoint = dev_data.args.checkpoint
        # load passage vectors
        index = dev_data.args.db_index
        if index==-1:
            for index in range(10):
                pvec_path = checkpoint[:checkpoint.index(".")] + ".psgs_w100_{}.npy".format(index)
                assert os.path.exists(pvec_path)
                if index==0:
                    pvec = np.load(pvec_path)
                else:
                    pvec = np.concatenate([pvec, np.load(pvec_path)], axis=0)
        else:
            pvec_path = checkpoint[:checkpoint.index(".")] + ".psgs_w100_{}.npy".format(index)
            print (pvec_path)
            if os.path.exists(pvec_path):
                pvec = np.load(pvec_path)
            else:
                dev_data.passages.load_tokenized_data("bert")
                dev_data.passages.load_dataset("bert")
                dataloader = dev_data.passages.load_dataloader(
                    dev_data.args.predict_batch_size,
                    is_training=False,
                    do_return=True)
                if dev_data.args.verbose:
                    dataloader = tqdm(dataloader)
                pvec = _inference(dataloader, is_passages=True)
                np.save(pvec_path, pvec)
            exit()
        print (pvec.shape)
        index = faiss.IndexFlatIP(pvec.shape[1])
        index.add(pvec)
        faiss.write_index(index, index_path)
        #with open(index_path, "wb") as f:
        #    writer = faiss.PyCallbackIOWriter(lambda x: 1234) #lambda b: os.write(f, b))
        #    faiss.write_index(index, writer)
    # load question vectors
    qvec = _inference(dev_data.dataloader, is_passages=False) #model.inference(dev_data.dataloader, is_passages=False)
    print (qvec.shape)
    D, I = index.search(qvec, 100)
    assert D.shape == I.shape == (qvec.shape[0], 100)
    predictions = I.tolist()
    accuracy = dev_data.passages.evaluate(predictions, dev_data.get_answers())
    if save_predictions:
        dev_data.save_predictions(predictions)
    return np.mean(accuracy)

def inference_seq2seq(model, dev_data, save_predictions=False):
    predictions = []
    #bos_token_id = model.config.bos_token_id if model.config.bos_token_id is not None \
    #    else model.config.decoder_start_token_id
    bos_token_id = dev_data.tokenizer.bos_token_id
    for i, batch in enumerate(dev_data.dataloader):
        with torch.no_grad():
            batch = [b.to(torch.device("cuda")) for b in batch]
            outputs = model.generate(input_ids=batch[0],
                                    attention_mask=batch[1],
                                    num_beams=4,
                                    max_length=20,
                                    early_stopping=True,)
                                    #use_cache=True,
                                    #no_repeat_ngram_size=4,)
                                    #decoder_start_token_id=bos_token_id)
            for input_, output in zip(batch[0], outputs):
                pred = dev_data.decode(output)
                if dev_data.args.verbose and len(predictions)<5:
                    print (dev_data.decode(input_)[:100])
                    print (pred)
                predictions.append(pred)
    if save_predictions:
        dev_data.save_predictions(predictions)
    return np.mean(dev_data.evaluate(predictions))

def inference_span_predictor(model, dev_data, save_predictions=False):
    outputs = []
    if dev_data.args.verbose:
        dev_data.dataloader = tqdm(dev_data.dataloader)
    for i, batch in enumerate(dev_data.dataloader):
        with torch.no_grad():
            batch = [b.to(torch.device("cuda")) for b in batch]
            batch_start_logits, batch_end_logits, batch_sel_logits = model(
                input_ids=batch[0], attention_mask=batch[1], token_type_ids=batch[2])
            batch_start_logits = batch_start_logits.detach().cpu().tolist()
            batch_end_logits = batch_end_logits.detach().cpu().tolist()
            batch_sel_logits = batch_sel_logits.detach().cpu().tolist()
            assert len(batch_start_logits)==len(batch_end_logits)==len(batch_sel_logits)
            for start_logit, end_logit, sel_logit in zip(batch_start_logits, batch_end_logits, batch_sel_logits):
                outputs.append((start_logit, end_logit, sel_logit))

    n_paragraphs = [int(n) for n in dev_data.args.n_paragraphs.split(",")] if save_predictions else None
    predictions = dev_data.decode_span(outputs,
                                       n_paragraphs=n_paragraphs)
    if save_predictions:
        dev_data.save_predictions(predictions)
    return np.mean(dev_data.evaluate(predictions, n_paragraphs=n_paragraphs))


