import os
import socket
import time
import sys
from pprint import pprint
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torch.distributed as dist
from torch.multiprocessing import spawn
from torch.nn.parallel import DistributedDataParallel
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from .data import FieldDataset, DistFieldSampler
from . import models
from .models import (
    narrow_cast, resample,lag2eul,
    WDistLoss, wasserstein_distance_loss, wgan_grad_penalty,
    grad_penalty_reg,
    add_spectral_norm,
    InstanceNoise,
)
from .utils import import_attr, load_model_state_dict, plt_slices, plt_power, score


ckpt_link = 'checkpoint.pt'


def node_worker(args):
    if 'SLURM_STEP_NUM_NODES' in os.environ:
        args.nodes = int(os.environ['SLURM_STEP_NUM_NODES'])
    elif 'SLURM_JOB_NUM_NODES' in os.environ:
        args.nodes = int(os.environ['SLURM_JOB_NUM_NODES'])
    else:
        raise KeyError('missing node counts in slurm env')
    args.gpus_per_node = torch.cuda.device_count()
    args.world_size = args.nodes * args.gpus_per_node

    node = int(os.environ['SLURM_NODEID'])

    if args.gpus_per_node < 1:
        raise RuntimeError('GPU not found on node {}'.format(node))
    
    print("method Node_worker in train.py")
    print("args",args)
    print("node",node)
    print("gpus per node", args.gpus_per_node)  
    spawn(gpu_worker, args=(node, args), nprocs=args.gpus_per_node)
    print("spawn successful")

def gpu_worker(local_rank, node, args):
    #device = torch.device('cuda', local_rank)
    #torch.cuda.device(device)  # env var recommended over this
	
    print("running gpu_worker in train.py")
	
    os.environ['CUDA_DEVICE_ORDER'] = 'PCI_BUS_ID'
    os.environ['CUDA_VISIBLE_DEVICES'] = str(local_rank)
    device = torch.device('cuda', 0)

    rank = args.gpus_per_node * node + local_rank

    # Need randomness across processes, for sampler, augmentation, noise etc.
    # Note DDP broadcasts initial model states from rank 0
    torch.manual_seed(args.seed + rank)
    # good practice to disable cudnn.benchmark if enabling cudnn.deterministic
    #torch.backends.cudnn.deterministic = True

    print("running dist_init in train.py")
    dist_init(rank, args)

    print("running FieldDataset in train.py")
    train_dataset = FieldDataset(
        in_patterns=args.train_in_patterns,
        tgt_patterns=args.train_tgt_patterns,
        style_pattern=args.train_style_pattern,
        in_norms=args.in_norms,
        tgt_norms=args.tgt_norms,
        callback_at=args.callback_at,
        augment=args.augment,
        aug_shift=args.aug_shift,
        aug_add=args.aug_add,
        aug_mul=args.aug_mul,
        crop=args.crop,
        crop_start=args.crop_start,
        crop_stop=args.crop_stop,
        crop_step=args.crop_step,
        in_pad=args.in_pad,
        tgt_pad=args.tgt_pad,
        scale_factor=args.scale_factor,
        **args.misc_kwargs,
    )
    print("running DistFieldSampler in train.py")
    train_sampler = DistFieldSampler(train_dataset, shuffle=True,
                                     div_data=args.div_data,
                                     div_shuffle_dist=args.div_shuffle_dist)
    #random_sampler = 
    print("running DataLoader in train.py")
    train_loader = DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        sampler=train_sampler,
        num_workers=args.loader_workers,
        pin_memory=True,
    )
    print("args.val =",args.val)
    if args.val:
        val_dataset = FieldDataset(
            in_patterns=args.val_in_patterns,
            tgt_patterns=args.val_tgt_patterns,
            style_pattern=args.val_style_pattern,
            in_norms=args.in_norms,
            tgt_norms=args.tgt_norms,
            callback_at=args.callback_at,
            augment=False,
            aug_shift=None,
            aug_add=None,
            aug_mul=None,
            crop=args.crop,
            crop_start=args.crop_start,
            crop_stop=args.crop_stop,
            crop_step=args.crop_step,
            in_pad=args.in_pad,
            tgt_pad=args.tgt_pad,
            scale_factor=args.scale_factor,
            **args.misc_kwargs,
        )
        val_sampler = DistFieldSampler(val_dataset, shuffle=False,
                                       div_data=args.div_data,
                                       div_shuffle_dist=args.div_shuffle_dist)
        val_loader = DataLoader(
            val_dataset,
            batch_size=args.batch_size,
            shuffle=False,
            sampler=val_sampler,
            num_workers=args.loader_workers,
            pin_memory=True,
        )

    args.in_chan = train_dataset.in_chan
    args.out_chan = train_dataset.tgt_chan
    args.style_size = train_dataset.style_size

    print("running import_attr in train.py")
    model = import_attr(args.model, models, callback_at=args.callback_at)
    model = model(sum(args.in_chan), sum(args.out_chan), style_size=args.style_size,
                  scale_factor=args.scale_factor, **args.misc_kwargs)
    model.to(device)
    print("running DistributedDataParallel in train.py")
    model = DistributedDataParallel(model, device_ids=[device],
                                    process_group=dist.new_group())

    criterion = import_attr(args.criterion, nn, models,
                            callback_at=args.callback_at)
    criterion = criterion()
    criterion.to(device)

    optimizer = import_attr(args.optimizer, optim, callback_at=args.callback_at)
    optimizer = optimizer(
        model.parameters(),
        lr=args.lr,
        **args.optimizer_args,
    )
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, **args.scheduler_args)

    adv_model = adv_criterion = adv_optimizer = adv_scheduler = None
    print("args.adv", args.adv)
    if args.adv:
        adv_model = import_attr(args.adv_model, models,
                                callback_at=args.callback_at)
        adv_model = adv_model(
            sum(args.in_chan + args.out_chan) if args.cgan
                else sum(args.out_chan),
            1,
            style_size=args.style_size,
            scale_factor=args.scale_factor,
            **args.misc_kwargs,
        )
        if args.adv_model_spectral_norm:
            add_spectral_norm(adv_model)
        adv_model.to(device)
        adv_model = DistributedDataParallel(adv_model, device_ids=[device],
                                            process_group=dist.new_group())

        adv_criterion = import_attr(args.adv_criterion, nn, models,
                                    callback_at=args.callback_at)
        adv_criterion = adv_criterion()
        adv_criterion.to(device)

        adv_optimizer = import_attr(args.optimizer, optim,
                                    callback_at=args.callback_at)
        adv_optimizer = adv_optimizer(
            adv_model.parameters(),
            lr=args.adv_lr,
            **args.adv_optimizer_args,
        )
        adv_scheduler = optim.lr_scheduler.ReduceLROnPlateau(
            adv_optimizer, **args.scheduler_args)

    print("args.load_state=", args.load_state)
    print("ckpt_link=",ckpt_link)
    print("args.load_state == ckpt_link",args.load_state == ckpt_link)
    print("os.path.isfile(ckpt_link)",os.path.isfile(ckpt_link))
    print("not args.load_state",not args.load_state,flush=True)

    if (args.load_state == ckpt_link and not os.path.isfile(ckpt_link)
            or not args.load_state):
        #if load_state is checkpoint.pt and if the path to checkpoint.py does NOT exist, or if load_state DNE.
        if args.init_weight_std is not None:
            model.apply(init_weights)

            if args.adv:
                adv_model.apply(init_weights)

        start_epoch = 0

        if rank == 0:
            min_loss = None
    else:
        state = torch.load(args.load_state, map_location=device)

        start_epoch = state['epoch']

        load_model_state_dict(model.module, state['model'],
                              strict=args.load_state_strict)

        if 'optimizer' in state:
            optimizer.load_state_dict(state['optimizer'])
        if 'scheduler' in state:
            scheduler.load_state_dict(state['scheduler'])

        if args.adv:
            if 'adv_model' in state:
                load_model_state_dict(adv_model.module, state['adv_model'],
                                      strict=args.load_state_strict)

            if 'adv_optimizer' in state:
                adv_optimizer.load_state_dict(state['adv_optimizer'])
            if 'adv_scheduler' in state:
                adv_scheduler.load_state_dict(state['adv_scheduler'])

        torch.set_rng_state(state['rng'].cpu())  # move rng state back

        if rank == 0:
            min_loss = state['min_loss']
            if args.adv and 'adv_model' not in state:
                min_loss = None  # restarting with adversary wipes the record

            print('state at epoch {} loaded from {}'.format(
                state['epoch'], args.load_state), flush=True)

        del state

    torch.backends.cudnn.benchmark = True

    if args.detect_anomaly:
        torch.autograd.set_detect_anomaly(True)
    
    print("running SummaryWriter in train.py")
    logger = None
    if rank == 0:
        logger = SummaryWriter()

    if rank == 0:
        print('pytorch {}'.format(torch.__version__))
        pprint(vars(args))
        sys.stdout.flush()

    if args.adv:
        args.instance_noise = InstanceNoise(args.instance_noise,
                                            args.instance_noise_batches)
    #print("start_epoch",start_epoch)
    for epoch in range(start_epoch, args.epochs):
        print("epoch",epoch)
        train_sampler.set_epoch(epoch)

        train_loss = train(epoch, train_loader,
            model, criterion, optimizer, scheduler,
            adv_model, adv_criterion, adv_optimizer, adv_scheduler,
            logger, device, args)
        epoch_loss = train_loss

        if args.val:
            val_loss = validate(epoch, val_loader,
                model, criterion, adv_model, adv_criterion,
                logger, device, args)
            #epoch_loss = val_loss

        if args.reduce_lr_on_plateau and epoch >= args.adv_start:
            scheduler.step(epoch_loss[0])
            if args.adv:
                adv_scheduler.step(epoch_loss[0])

        if rank == 0:
            logger.flush()

            if ((min_loss is None or epoch_loss[0] < min_loss[0])
                    and epoch >= args.adv_start):
                min_loss = epoch_loss

            state = {
                'epoch': epoch + 1,
                'model': model.module.state_dict(),
                'optimizer': optimizer.state_dict(),
                'scheduler': scheduler.state_dict(),
                'rng': torch.get_rng_state(),
                'min_loss': min_loss,
            }
            if args.adv:
                state.update({
                    'adv_model': adv_model.module.state_dict(),
                    'adv_optimizer': adv_optimizer.state_dict(),
                    'adv_scheduler': adv_scheduler.state_dict(),
                })

            state_file = 'state_{}.pt'.format(epoch + 1)
            torch.save(state, state_file)
            del state

            tmp_link = '{}.pt'.format(time.time())
            os.symlink(state_file, tmp_link)  # workaround to overwrite
            os.rename(tmp_link, ckpt_link)

    dist.destroy_process_group()


def train(epoch, loader, model, criterion, optimizer, scheduler,
        adv_model, adv_criterion, adv_optimizer, adv_scheduler,
        logger, device, args):
    model.train()
    if args.adv:
        adv_model.train()
    print(torch.version.cuda, '------ cuda version -------')
    rank = dist.get_rank()
    world_size = dist.get_world_size()

    if (args.log_interval <= args.adv_wgan_gp_interval
        or args.adv_wgan_gp_interval < 1):
        adv_wgan_gp_log_interval = args.log_interval
    else:
        adv_wgan_gp_log_interval = (
            args.log_interval // args.adv_wgan_gp_interval
            * args.adv_wgan_gp_interval)

    # loss, loss_adv, adv_loss, adv_loss_fake, adv_loss_real
    # loss: generator (model) supervised loss
    # loss_adv: generator (model) adversarial loss
    # adv_loss: discriminator (adv_model) loss
    epoch_loss = torch.zeros(5, dtype=torch.float64, device=device)
    fake = torch.zeros([1], dtype=torch.float32, device=device)
    real = torch.ones([1], dtype=torch.float32, device=device)
    adv_real = torch.full([1], args.adv_label_smoothing, dtype=torch.float32,
            device=device)

    print("Loader_len: ",len(loader))
    for i, data in enumerate(loader):
        batch = epoch * len(loader) + i + 1

        #BAYU 240117
        print("Epoch: {}, Batch: {}".format(epoch,batch),flush=True)
        #print("epoch : ", epoch)
        #print("i : ",i)
        #print("\n",flush=True)
        #BAYU 240117
        input, target, style = data['input'], data['target'], data['style']

        #print(f"input = input.to(device, non_blocking=True), Device: {device}",flush=True)
        input = input.to(device, non_blocking=True)
        #print("target = target.to(device, non_blocking=True), Device: {device}",flush=True)        
        target = target.to(device, non_blocking=True)
        style = style.to(device, non_blocking=True)
        
        #print(input.shape, style.shape)
        output = model(input, style)
        #print("output = model(input, style)",flush=True)
        if batch <= 5 and rank == 0:
            print('##### batch :', batch)
            print('input shape :', input.shape)
            print('output shape :', output.shape)
            print('target shape :', target.shape)
            print('style shape :', style.shape)

        if (hasattr(model.module, 'scale_factor')
                and model.module.scale_factor != 1):
            input = resample(input, model.module.scale_factor, narrow=False)
        input, output, target = narrow_cast(input, output, target)
        if batch <= 5 and rank == 0:
            print('narrowed shape :', output.shape, flush=True)

        loss = criterion(output, target)
        # print('----- after trainin criterion -----')
        # print(output.requires_grad, 'check require output gradient in training')
        # print(target.requires_grad, 'check require target gradient in training')
        epoch_loss[0] += loss.detach()

        if args.adv and epoch >= args.adv_start:
            noise_std = args.instance_noise.std()
            if noise_std > 0:
                noise = noise_std * torch.randn_like(output)
                output = output + noise
                noise = noise_std * torch.randn_like(target)
                target = target + noise
                del noise

            lag_out = output[:, :3]
            eul_out = lag2eul(lag_out, a=np.float(style))[0]
            lag_tgt = target[:, :3]
            eul_tgt = lag2eul(lag_tgt, a=np.float(style))[0]
            
            output = torch.cat([eul_out, output], dim=1)
            target = torch.cat([eul_tgt, target], dim=1)
            
            
            if args.cgan:
                output = torch.cat([input, output], dim=1)
                target = torch.cat([input, target], dim=1)
            # if output.requires_grad is not True:
            #     output.requires_grad_(True)
            # if target.requires_grad is not True:
            #     target.requires_grad_(True)
            # print('----- after set requires grad -----')
            # print(output.requires_grad, 'check require output gradient in training')
            # print(target.requires_grad, 'check require target gradient in training')
                
            # check require grad
            # assert target.requires_grad == True
            # assert output.requires_grad == True
            # discriminator
            set_requires_grad(adv_model, True)

            score_out = adv_model(output.detach(), style=style)
            adv_loss_fake = adv_criterion(score_out, fake.expand_as(score_out))
            epoch_loss[3] += adv_loss_fake.item()


            adv_optimizer.zero_grad()
            adv_loss_fake.backward()

            score_tgt = adv_model(target, style=style)
            adv_loss_real = adv_criterion(score_tgt, adv_real.expand_as(score_tgt))
            epoch_loss[4] += adv_loss_real.item()


            adv_loss_real.backward()

            adv_loss = adv_loss_fake + adv_loss_real
            epoch_loss[2] += adv_loss.item()

            if (args.adv_wgan_gp_interval > 0
                and  batch % args.adv_wgan_gp_interval == 0):
                adv_loss_reg = wgan_grad_penalty(adv_model, output, target, style=style)
                adv_loss_reg_ = adv_loss_reg * args.adv_wgan_gp_interval

                adv_loss_reg_.backward()

                if batch % adv_wgan_gp_log_interval == 0 and rank == 0:
                    logger.add_scalar(
                        'loss/batch/train/adv/reg',
                        adv_loss_reg.item(),
                        global_step=batch,
                    )

            adv_optimizer.step()
            adv_grads = get_grads(adv_model)

            # generator adversarial loss
            if batch % args.adv_iter_ratio == 0:
                set_requires_grad(adv_model, False)

                score_out = adv_model(output, style=style)
                loss_adv = adv_criterion(score_out, real.expand_as(score_out))
                epoch_loss[1] += args.adv_iter_ratio * loss_adv.item()

                optimizer.zero_grad()
                loss_adv.backward()
                optimizer.step()
                grads = get_grads(model)
        else:
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            grads = get_grads(model)

        if batch % args.log_interval == 0:
            dist.all_reduce(loss)
            loss /= world_size
            if rank == 0:
                logger.add_scalar('loss/batch/train', loss.item(),
                                  global_step=batch)
                if args.adv and epoch >= args.adv_start:
                    logger.add_scalar('loss/batch/train/adv/G', loss_adv.item(),
                                      global_step=batch)
                    logger.add_scalars(
                        'loss/batch/train/adv/D',
                        {
                            'total': adv_loss.item(),
                            'fake': adv_loss_fake.item(),
                            'real': adv_loss_real.item(),
                        },
                        global_step=batch,
                    )

                logger.add_scalar('grad/first', grads[0], global_step=batch)
                logger.add_scalar('grad/last', grads[-1], global_step=batch)
                if args.adv and epoch >= args.adv_start:
                    logger.add_scalar('grad/adv/first', adv_grads[0],
                                      global_step=batch)
                    logger.add_scalar('grad/adv/last', adv_grads[-1],
                                      global_step=batch)

                    if noise_std > 0:
                        logger.add_scalar('instance_noise', noise_std,
                                          global_step=batch)

    dist.all_reduce(epoch_loss)
    epoch_loss /= len(loader) * world_size
    if rank == 0:
        logger.add_scalar('loss/epoch/train', epoch_loss[0],
                          global_step=epoch+1)
        if args.adv and epoch >= args.adv_start:
            logger.add_scalar('loss/epoch/train/adv/G', epoch_loss[1],
                              global_step=epoch+1)
            logger.add_scalars(
                'loss/epoch/train/adv/D',
                {
                    'total': epoch_loss[2],
                    'fake': epoch_loss[3],
                    'real': epoch_loss[4],
                },
                global_step=epoch+1,
            )

        if args.adv and epoch >= args.adv_start and args.cgan:
            skip_chan = sum(args.in_chan)
            output = output[:, skip_chan:]
            target = target[:, skip_chan:]

            print('input shape :', input.shape, flush=True)
            print('target shape :', target.shape, flush=True)
            print("skip chan:",skip_chan)
        # metric_score = score(
        #     output, target,
        #     labels = ['output', 'target'],
        # )

        # logger.add_scalar('loss/epoch/train/score', metric_score, global_step=epoch+1)

        print('------input shape before power--------', input.shape)
        print('------output shape before power--------', output.shape)
        print('------target shape before power--------', target.shape)

	#Bayu only plot last 6 channels, 23/09/22
        fig = plt_slices(
            #input[-1], output[-1], target[-1], output[-1] - target[-1],
            input[-1][-6:-3], output[-1][-6:-3], target[-1][-6:-3], output[-1][-6:-3] - target[-1][-6:-3],
            title=['in', 'out', 'tgt', 'out - tgt'],
            **args.misc_kwargs,
        )
        logger.add_figure('fig/train/disp', fig, global_step=epoch+1)
        fig.clf()

        fig = plt_slices(
            #input[-1], output[-1], target[-1], output[-1] - target[-1],
            input[-1][-3:], output[-1][-3:], target[-1][-3:], output[-1][-3:] - target[-1][-3:],
            title=['in', 'out', 'tgt', 'out - tgt'],
            **args.misc_kwargs,
        )
        logger.add_figure('fig/train/vel', fig, global_step=epoch+1)
        fig.clf()

        #if epoch%args.ps_interval == 0:
        #    logger.add_figure('fig/epoch/val_ps',lr2sr_Ps(args.lr_disp_path,args.tgt_ps_path,args.lr_ps_path,\
        #                                             model,args.scale_factor,args.pad,args.Lbox),global_step=epoch+1)
        #fig = plt_power(
        #    input, output, target,
        #    label=['in', 'out', 'tgt'],
        #    **args.misc_kwargs,
        #)
        #logger.add_figure('fig/train/power/lag', fig, global_step=epoch+1)
        #fig.clf()
        #torch.cuda.memory_snapshot()

        fig = plt_power(1.0,
            dis=[input[:,-6:-3,:,:,:], output[:,-6:-3,:,:,:], target[:,-6:-3,:,:,:]],
            label=['in', 'out', 'tgt'],
            **args.misc_kwargs,
        )
        logger.add_figure('fig/train/power/eul/disp', fig, global_step=epoch+1)
        fig.clf()

        #fig = plt_power(1.0,
        #    dis=[input[:,-3:,:,:,:], input[:,-3:,:,:,:], input[:,-3:,:,:,:]],
        #    label=['in', 'out', 'tgt'],
        #    **args.misc_kwargs,
        #)
        #logger.add_figure('fig/train/power/eul/vel', fig, global_step=epoch+1)
        #fig.clf()

        #fig = plt_power(1.0,
        #    dis=[input, output, target],
        #    label=['in', 'out', 'tgt'],
        #    **args.misc_kwargs,
        #)
        #logger.add_figure('fig/train/power/eul', fig, global_step=epoch+1)
        #fig.clf()

    return epoch_loss


def validate(epoch, loader, model, criterion, adv_model, adv_criterion,
        logger, device, args):
    model.eval()
    if args.adv:
        adv_model.eval()

    rank = dist.get_rank()
    world_size = dist.get_world_size()

    epoch_loss = torch.zeros(5, dtype=torch.float64, device=device)
    fake = torch.zeros([1], dtype=torch.float32, device=device)
    real = torch.ones([1], dtype=torch.float32, device=device)

    with torch.no_grad():
        for data in loader:
            input, target, style = data['input'], data['target'], data['style']

            input = input.to(device, non_blocking=True)
            target = target.to(device, non_blocking=True)
            style = style.to(device, non_blocking=True)

            output = model(input, style=style)

            if (hasattr(model.module, 'scale_factor')
                    and model.module.scale_factor != 1):
                input = resample(input, model.module.scale_factor, narrow=False)
            input, output, target = narrow_cast(input, output, target)

            loss = criterion(output, target)
            epoch_loss[0] += loss.detach()

            if args.adv and epoch >= args.adv_start:
                if args.cgan:
                    output = torch.cat([input, output], dim=1)
                    target = torch.cat([input, target], dim=1)

                # discriminator
                score_out = adv_model(output, style=style)
                adv_loss_fake = adv_criterion(score_out, fake.expand_as(score_out))
                epoch_loss[3] += adv_loss_fake.detach()

                score_tgt = adv_model(target, style=style)
                adv_loss_real = adv_criterion(score_tgt, real.expand_as(score_tgt))
                epoch_loss[4] += adv_loss_real.detach()

                adv_loss = adv_loss_fake + adv_loss_real
                epoch_loss[2] += adv_loss.detach()

                # generator adversarial loss
                loss_adv = adv_criterion(score_out, real.expand_as(score_out))
                epoch_loss[1] += loss_adv.detach()

    dist.all_reduce(epoch_loss)
    epoch_loss /= len(loader) * world_size
    if rank == 0:
        logger.add_scalar('loss/epoch/val', epoch_loss[0],
                          global_step=epoch+1)
        if args.adv and epoch >= args.adv_start:
            logger.add_scalar('loss/epoch/val/adv/G', epoch_loss[1],
                              global_step=epoch+1)
            logger.add_scalars(
                'loss/epoch/val/adv/D',
                {
                    'total': epoch_loss[2],
                    'fake': epoch_loss[3],
                    'real': epoch_loss[4],
                },
                global_step=epoch+1,
            )

        if args.adv and epoch >= args.adv_start and args.cgan:
            skip_chan = sum(args.in_chan)
            output = output[:, skip_chan:]
            target = target[:, skip_chan:]

        fig = plt_slices(
            input[-1], output[-1], target[-1], output[-1] - target[-1],
            title=['in', 'out', 'tgt', 'out - tgt'],
            **args.misc_kwargs,
        )
        logger.add_figure('fig/val', fig, global_step=epoch+1)
        fig.clf()

        fig = plt_power(
            input, output, target,
            label=['in', 'out', 'tgt'],
            **args.misc_kwargs,
        )
        logger.add_figure('fig/val/power/lag', fig, global_step=epoch+1)
        fig.clf()

        #fig = plt_power(1.0,
        #    dis=[input, output, target],
        #    label=['in', 'out', 'tgt'],
        #    **args.misc_kwargs,
        #)
        #logger.add_figure('fig/val/power/eul', fig, global_step=epoch+1)
        #fig.clf()

    return epoch_loss


def dist_init(rank, args):
    dist_file = 'dist_addr'

    if rank == 0:
        addr = socket.gethostname()

        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((addr, 0))
            _, port = s.getsockname()

        args.dist_addr = 'tcp://{}:{}'.format(addr, port)

        with open(dist_file, mode='w') as f:
            f.write(args.dist_addr)
    else:
        while not os.path.exists(dist_file):
            time.sleep(1)

        with open(dist_file, mode='r') as f:
            args.dist_addr = f.read()

    dist.init_process_group(
        backend=args.dist_backend,
        init_method=args.dist_addr,
        world_size=args.world_size,
        rank=rank,
    )
    dist.barrier()

    if rank == 0:
        os.remove(dist_file)


def init_weights(m):
    if isinstance(m, (nn.Linear, nn.Conv1d, nn.Conv2d, nn.Conv3d,
        nn.ConvTranspose1d, nn.ConvTranspose2d, nn.ConvTranspose3d)):
        m.weight.data.normal_(0.0, args.init_weight_std)
    elif isinstance(m, (nn.BatchNorm1d, nn.BatchNorm2d, nn.BatchNorm3d,
        nn.SyncBatchNorm, nn.LayerNorm, nn.GroupNorm,
        nn.InstanceNorm1d, nn.InstanceNorm2d, nn.InstanceNorm3d)):
        if m.affine:
            # NOTE: dispersion from DCGAN, why?
            m.weight.data.normal_(1.0, args.init_weight_std)
            m.bias.data.fill_(0)


def set_requires_grad(module, requires_grad=False):
    for param in module.parameters():
        param.requires_grad = requires_grad


def get_grads(model):
    """gradients of the weights of the first and the last layer
    """
    grads = list(p.grad for n, p in model.named_parameters()
                 if '.weight' in n)
    grads = [grads[0], grads[-1]]
    grads = [g.detach().norm() for g in grads]
    return grads
