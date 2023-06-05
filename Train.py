
# import packages
import argparse
import pickle
import os
import torch
import torch.nn as nn
import torch.optim as optim
import torch.utils.data as data
from src.LoadDataBPE import LoadDataBPE
from src.LoadDataSimple import LoadDataSimple
from src.LoadDataBase import PytorchCustomLoader
from src.TransformerModelAuto import AutomaticTransformer
from src.TransformerModelManual import ManualTransformer
from sacrebleu.metrics import BLEU


def calculate_BLEU(
        targets: list,
        predictions: list,
        int2target: dict
) -> float:

    # targets, predictions are lists of tensors (for every pair of target - pred outputs)
    target_symbols, prediction_symbols = [], []

    for i , (target, prediction) in enumerate(zip(targets, predictions)):

        target_in_symbol = ' '.join([int2target[i.item()] for i in target[:-1]])
        target_symbols += [target_in_symbol]

        prediction_in_symbol = ' '.join([int2target[i.item()] for i in prediction[:-1]])
        prediction_symbols += [prediction_in_symbol]

    bleu = BLEU().corpus_score(hypotheses=prediction_symbols, references=[target_symbols])
    return bleu.score


def test(
        test_loader: data.DataLoader,
        model: nn.Module,
        device: torch.device,
        int2target: dict
) -> float:

    targets, predictions = [], []

    for source, target in test_loader:

        source, tagret = source.to(device), target.to(device)

        _, prediction = model(source, target)

        target = target.reshape(-1)
        prediction = prediction.reshape(-1)

        predictions += [prediction]
        targets += [target]

    return calculate_BLEU(targets, predictions, int2target=int2target)


def evaluate(
        dev_loader: data.DataLoader,
        model: nn.Module,
        criterion: nn.CrossEntropyLoss,
        device: torch.device,
        int2target: dict
) -> tuple:

    loss_set = total = 0
    targets, predictions = [], []

    for source, target in dev_loader:

        source, target = source.to(device), target.to(device)

        output, prediction = model(source, target)

        target = target.reshape(-1)
        output = output.reshape(-1, output.shape[2])
        prediction = prediction.reshape(-1)

        targets += [target]
        predictions += [prediction]

        loss = criterion(output, target)
        loss_set += loss.item()
        total += source.shape[0]

    bleu_set = calculate_BLEU(targets, predictions, int2target=int2target)

    return loss_set/total, bleu_set


def train(
        train_loader: data.DataLoader,
        dev_loader: data.DataLoader,
        learning_rate: float,
        max_iter: int,
        model: nn.Module,
        int2target: dict,
        device: torch.device,
        checkpoint_path: str
) -> tuple:

    # set optimizer and loss
    opt = optim.Adam(model.parameters(), lr=learning_rate)
    criterion = nn.CrossEntropyLoss()

    # set history
    train_loss, train_bleu, dev_loss, dev_bleu = [], [], [], []
    model.train()

    # best bleu
    best_bleu = 0
    print("starts training...")

    for i in range(max_iter):

        epoch_loss = total = 0
        targets, predictions = [], []

        for j, (source, target) in enumerate(train_loader):

            source, target = source.to(device), target.to(device)
            opt.zero_grad()

            # output: (batch_size, trg_len, trg_vocab), prediction: (batch_size, trg_len)
            output, prediction = model(source, target)

            target = target.reshape(-1)
            output = output.reshape(-1, output.shape[2])
            prediction = prediction.reshape(-1)

            targets += [target]
            predictions += [prediction]

            loss = criterion(output, target)
            loss.backward()
            opt.step()

            epoch_loss += loss.item()
            total += source.shape[0]

        # calculate BLEU on examples
        bleu_epoch = calculate_BLEU(targets, predictions, int2target=int2target)

        # evaluate on development
        loss_dev, bleu_dev = evaluate(dev_loader, model=model, device=device, criterion=criterion, int2target=int2target)

        # update history
        train_bleu += [bleu_epoch]
        train_loss += [epoch_loss/total]
        dev_loss += [loss_dev]
        dev_bleu += [bleu_dev]

        print("eopch: {}, train loss: {}, dev loss: {}, train BLEU: {}, dev BLEU: {}".format(
            i, epoch_loss/total, loss_dev, bleu_epoch, bleu_dev
        ))

        # break? save model
        if bleu_dev > best_bleu:
            best_bleu = bleu_dev
            checkpoint_dict = {'model': model.state_dict(), 'criterion': criterion.state_dict()}
            torch.save(checkpoint_dict, checkpoint_path)

    return (train_loss, train_bleu, dev_loss, dev_bleu)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-s', '--SourcePath', required=True, type=str, help='path to source file')
    parser.add_argument('-t', '--TargetPath', required=True, type=str, help='path to target file')
    parser.add_argument('-d', '--Debug', default=0, type=int)
    parser.add_argument('-a', '--AutomaticModel', default=1, type=int)
    parser.add_argument('-b', '--BPE', default=1, type=int)
    args = parser.parse_args()

    # hyper-parameters
    max_iter = 3
    lr = 0.001
    dropout = 0.0
    batch_size = 1
    max_vocab = 10 #1000
    embedding_dim = 256
    num_heads = 2 #8
    n_encoder_blocks = 2 #4
    n_decoder_blocks = 2 #4
    feed_forward_size = 32
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    symbol_kwargs = {
        'SOS': '<SOS>',
        'EOS': '<EOS>',
        'UNK': '<UNK>',
        'PAD': '<PAD>',
        'EOW': '</w>',
        'EOC': '</c>'
    }

    # choose simple or BPE tokenizer
    if args.BPE:
        source_loader = LoadDataBPE(file_path=args.SourcePath, debug=args.Debug)
        target_loader = LoadDataBPE(file_path=args.TargetPath, debug=args.Debug)
    else:
        source_loader = LoadDataSimple(file_path=args.SourcePath, debug=args.Debug)
        target_loader = LoadDataSimple(file_path=args.TargetPath, debug=args.Debug)

    # load source sentences
    sources = source_loader.readFile()
    s2i, i2s = source_loader.createVocab(max_vocab=max_vocab, **symbol_kwargs)
    sources = source_loader.tokenize(w2i=s2i, sentences=sources, eow=symbol_kwargs['EOW']).getTokenizedSentences()
    max_src_len = source_loader.getMaxSentenceLength()

    # load target sentences
    targets = target_loader.readFile()
    t2i, i2t = target_loader.createVocab(max_vocab=max_vocab, **symbol_kwargs)
    targets = target_loader.tokenize(w2i=t2i, sentences=targets, eow=symbol_kwargs['EOW']).getTokenizedSentences()
    max_trg_len = target_loader.getMaxSentenceLength()

    # split to sets
    assert len(targets) == len(sources)
    num_examples = len(targets)
    size_train = int(num_examples*.85)
    train_targets, train_sources = targets[:size_train], sources[:size_train]
    dev_targets, dev_sources = targets[size_train:], sources[size_train:]

    # load to pytorch tensors
    train_loader = PytorchCustomLoader(
        sources=train_sources, targets=train_targets, bpe_tokenizer=bool(args.BPE), sources2int=s2i, targets2int=t2i, **symbol_kwargs
    )
    train_loader = data.DataLoader(dataset=train_loader, batch_size=batch_size, shuffle=True)

    dev_loader = PytorchCustomLoader(
        sources=dev_sources, targets=dev_targets, bpe_tokenizer=bool(args.BPE), sources2int=s2i, targets2int=t2i, **symbol_kwargs
    )
    dev_loader = data.DataLoader(dataset=dev_loader, batch_size=batch_size, shuffle=True)

    # initialize model
    model_kwargs = {
        'embedding_dim': embedding_dim,
        'num_heads': num_heads,
        'N_encoder_blocks': n_encoder_blocks,
        'N_decoder_blocks': n_decoder_blocks,
        'ff_dim': feed_forward_size,
        'device': device,
        'dropout': dropout,
        'src_vocab_size': len(s2i),
        'src_max_size': max_src_len,
        'trg_vocab_size': len(t2i),
        'trg_max_size': max_trg_len
    }
    if args.AutomaticModel:
        model = AutomaticTransformer(**model_kwargs)
    else:
        model = ManualTransformer(**model_kwargs)

    # save kwargs used (for loading)
    checkpoint_path = os.path.join(os.getcwd(), 'checkpoints')
    if not os.path.isdir(checkpoint_path):
        os.mkdir(checkpoint_path)
    with open(os.path.join(checkpoint_path, 'model_kwargs'), 'wb+') as f:
        pickle.dump(model_kwargs, f)
    environment_kwargs = {"batch_size": batch_size, "s2i": s2i, "i2s": i2s, "t2i": t2i, "i2t":i2t}
    environment_kwargs.update(symbol_kwargs)
    with open(os.path.join(checkpoint_path, 'env_kwargs'), 'wb+') as f:
        pickle.dump(environment_kwargs, f)

    # train model
    history = train(
        train_loader=train_loader,
        dev_loader=dev_loader,
        learning_rate=lr,
        device=device,
        model=model,
        max_iter=max_iter,
        int2target=i2t,
        checkpoint_path=os.path.join(checkpoint_path, 'checkpoint')
    )


if __name__ == "__main__":
    main()