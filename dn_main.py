import argparse
import os
import shutil
import time
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import densenet as dn
import vgg
import numpy as np
import copy
import torch.nn.init as init

model_names = sorted(name for name in vgg.__dict__
    if name.islower() and not name.startswith("__")
                     and name.startswith("vgg")
                     and callable(vgg.__dict__[name]))

parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
parser.add_argument('--arch', '-a', metavar='ARCH', default='vgg19',
                    choices=model_names,
                    help='model architecture: ' + ' | '.join(model_names) +
                    ' (default: vgg19)')
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=1000, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=4096,  type = int,
                    metavar='N', help='mini-batch size (default: 128)')
parser.add_argument('--lr', '--learning-rate', default=0.5, type=float,
                    metavar='LR', help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--weight-decay', '--wd', default=5e-4, type=float,
                    metavar='W', help='weight decay (default: 5e-4)')
parser.add_argument('--print-freq', '-p', default=5, type=int,
                    metavar='N', help='print frequency (default: 20)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--pretrained', dest='pretrained', action='store_true',
                    help='use pre-trained model')
parser.add_argument('--half', dest='half', action='store_true',
                    help='use half-precision(16-bit) ')
parser.add_argument('--save-dir', dest='save_dir',
                    help='The directory used to save the trained models',
                    default='save_temp', type=str)
parser.add_argument('--lr_cubic', default=0.00001, type=float, help='learning rate for cubic')
parser.add_argument('--rc', default=0.0001, type=float, help='cauchy point')
parser.add_argument('--cubic_epoch', default=5, type=float, help='cubic epoch')
parser.add_argument('--rho', default=1.0, type=float, help='second-order smoothness')
parser.add_argument('--cubic-batch-size', default=128, type=int,
                    metavar='N', help='mini-batch size (default: 128)')
parser.add_argument('--cubic-weight-decay', default=5e-4, type=float,
                    metavar='W', help='weight decay (default: 5e-4)')
parser.add_argument('--cubic-momentum', default=0.0, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--approx_delta', default=0.0001, type=float,
                    help='approximate parameter for Hessian-vector product')


best_prec1 = 0
args = parser.parse_args()
print(args)

# variable for evaluation of cubic subproblem
m_deltas = []

normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                 std=[0.229, 0.224, 0.225])
trainset = datasets.CIFAR10(root='./data', train=True, transform=transforms.Compose([
        transforms.RandomHorizontalFlip(),
        transforms.RandomCrop(32, 4),
        transforms.ToTensor(),
        normalize,
    ]), download=True)
testset = datasets.CIFAR10(root='./data', train=False, transform=transforms.Compose([
            transforms.ToTensor(),
            normalize,
        ]))

train_loader = torch.utils.data.DataLoader(
    trainset,
    batch_size=args.batch_size, shuffle=True,
    num_workers=args.workers, pin_memory=True)

trainloader_cubic_hv = torch.utils.data.DataLoader(
    trainset,
    batch_size=args.cubic_batch_size, shuffle=True,
    num_workers=args.workers, pin_memory=True)

val_loader = torch.utils.data.DataLoader(
    testset,
    batch_size=args.cubic_batch_size, shuffle=False,
    num_workers=args.workers)

def xavier(param):
    if param.ndimension() > 1:
        init.xavier_uniform(param)

def weights_init(m):
    if isinstance(m, nn.Conv2d):
        xavier(m.weight.data)
        xavier(m.bias.data)

def main():
    global args, best_prec1

    # Check the save_dir exists or not
    if not os.path.exists(args.save_dir):
        os.makedirs(args.save_dir)

    model = dn.DenseNet3(20, 10, 12, bottleneck=False, dropRate=0.0)

    model.cuda()
    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            best_prec1 = checkpoint['best_prec1']
            model.load_state_dict(checkpoint['state_dict'])
            print(("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.evaluate, checkpoint['epoch'])))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    cudnn.benchmark = True

    # define loss function (criterion) and pptimizer
    criterion = nn.CrossEntropyLoss().cuda()

    if args.half:
        model.half()
        criterion.half()

    optimizer = torch.optim.SGD(model.parameters(), args.lr, momentum=args.momentum, weight_decay=0.0005)

    if args.evaluate:
        validate(val_loader, model, criterion)
        return

    # scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[50,100,150,200], gamma=0.5)

    for epoch in range(args.start_epoch, args.epochs):
        # scheduler.step()
        adjust_learning_rate(optimizer, epoch)

        # train for one epoch
        cubic_train(model, criterion, optimizer, epoch)

        # evaluate on validation set
        prec1 = validate(val_loader, model, criterion)

        # remember best prec@1 and save checkpoint
        is_best = prec1 > best_prec1
        best_prec1 = max(prec1, best_prec1)
        # save_checkpoint({
        #     'epoch': epoch + 1,
        #     'state_dict': model.state_dict(),
        #     'best_prec1': best_prec1,
        # }, is_best, filename=os.path.join(args.save_dir, 'checkpoint_{}.tar'.format(epoch)))


def net_split_forward(net, criterion, inputs, targets, split_thresh=512):
    # inputs: Variable on CPU
    # targets: Variable on GPU

    # split parameter
    splits = 16
    batch_size = inputs.size(0)
    grad_avg = []

    if batch_size > split_thresh:
        assert batch_size % splits == 0
        quarter = batch_size // splits
        outputs = []
        loss = 0.0
        for i in range(splits):
            inputs1, targets1 = inputs[i*quarter:(i+1)*quarter], targets[i*quarter:(i+1)*quarter]
            outputs1 = net.forward(inputs1.cuda())
            outputs.append(outputs1)
            loss1 = criterion(outputs1, targets1.cuda())
            loss += loss1
            if i == 0:
                grad_avg = torch.autograd.grad(loss1, net.parameters())
                for p_avg in grad_avg:
                    p_avg.data = 1.0 * p_avg.data / splits
            else:
                grad_temp = torch.autograd.grad(loss1, net.parameters())
                for p_avg, p_temp in zip(grad_avg, grad_temp):
                    p_avg.data += 1.0 * p_temp.data / splits

        loss = 1.0 * loss / splits
        outputs = torch.cat(outputs, dim=0)
    else:
        outputs = net.forward(inputs.cuda())
        loss = criterion(outputs, targets.cuda())
        grad_avg = torch.autograd.grad(loss, net.parameters())

    return outputs, loss, grad_avg


def clip_gradient(optimizer, grad_clip):
    for group in optimizer.param_groups:
        for param in group['params']:
            param.grad.data.clamp_(-grad_clip, grad_clip)


def cubic_approx(net, epoch, optimizer, criterion, inputs_grad, targets_grad, inputs_hv, targets_hv):
    net.train()

    # zero the gradient
    optimizer.zero_grad()
    # g_t
    # Record the sub-sampled gradient
    # outputs_grad, loss_grad = net_split_forward(net, criterion, inputs_grad, targets_grad)
    # grad_cubic = torch.autograd.grad(loss_grad, net.parameters())
    outputs_grad, loss_grad, grad_cubic = net_split_forward(net, criterion, inputs_grad, targets_grad)

    # Initialize the iterate, i.e., Delta
    net_delta = copy.deepcopy(net)

    net_delta_optimizer = torch.optim.SGD(net_delta.parameters(), lr=args.lr_cubic, momentum=0.9, weight_decay=0.0)

    norm_delta = 0.0
    for p_delta, p_cubic_grad in zip(net_delta.parameters(), grad_cubic):
        p_delta.data = copy.deepcopy(p_cubic_grad.data)
        norm_delta += p_cubic_grad.data.norm(2) ** 2
    norm_delta = np.sqrt(norm_delta)
    norm_grad_cubic = norm_delta

    # print("norm_net_delta_grad, 0, begin ", norm_delta)

    for p in net_delta.parameters():
        p.data /= (- 1.0 / args.rc)

    # set net() and net_delta() to eval() status
    net.eval()
    net_delta.eval()

    # Calculate gradient on net_hv
    optimizer.zero_grad()
    outputs_hv = net.forward(inputs_hv)
    loss_hv = criterion(outputs_hv, targets_hv)
    grad_hv = torch.autograd.grad(loss_hv, net.parameters(), create_graph=True)

    # Gradient Descent for solving cubic sub-problem
    for epoch_c in range(args.cubic_epoch):

        net_delta_optimizer.zero_grad()
        net_delta_loss = criterion(net_delta.forward(inputs_hv[:10]), targets_hv[:10])
        net_delta_loss.backward()

        inner_product = 0.0
        for p_delta, p_grad_hv in zip(net_delta.parameters(), grad_hv):
            inner_product += torch.sum(p_delta * p_grad_hv)

        # hv_exact: H*Delta_t
        hv_exact = torch.autograd.grad(inner_product, net.parameters(), create_graph=True)

        for p_delta_net, p_hv in zip(net_delta.parameters(), hv_exact):
            p_delta_net.grad.data = p_hv.data * 1.0

        # clip H*v
        torch.nn.utils.clip_grad_norm(net_delta.parameters(), 5.0)

        # Calculate norm of iterate, i.e., ||\Delta||
        norm_delta = 0.0
        for p in net_delta.parameters():
            norm_delta += p.data.norm(2) ** 2
        norm_delta = float(np.sqrt(norm_delta))

        # grad = g_t + H*Delta_t + ||Delta||*Delta
        # Take a gradient step
        for params_delta_net, params_grad_cubic in zip(net_delta.parameters(), grad_cubic):
            params_delta_net.grad.data += params_grad_cubic.data * 1.0
            params_delta_net.grad.data += params_delta_net.data * args.rho * norm_delta * 1.0

        # clipping the gradient
        torch.nn.utils.clip_grad_norm(net_delta.parameters(), 10.0)

        net_delta_optimizer.step()

    # Take a cubic step
    # net generate dummy grad
    optimizer.zero_grad()
    outputs_grad_dummy = net.forward(inputs_grad[:10].cuda())
    loss_grad_dummy = criterion(outputs_grad_dummy, targets_grad[:10])
    loss_grad_dummy.backward()

    # calculate inner product
    norm_delta_approx = 0.0
    for p in net_delta.parameters():
        norm_delta_approx += p.data.norm(2) ** 2
    norm_delta_approx = float(np.sqrt(norm_delta_approx))

    inner_product = 0.0
    for p_cubic_grad, p_delta_approx in zip(grad_cubic, net_delta.parameters()):
        inner_product -= torch.sum(p_cubic_grad * p_delta_approx)
    cosin = (inner_product / (norm_delta_approx * norm_grad_cubic)).data[0]
    # print('cosine between delta and grad_cubic: {:.8f}'.format(cosin))

    for param_net, param_delta in zip(net.parameters(), net_delta.parameters()):
        param_net.grad.data = - 1.0 * param_delta.data * 1e3

    # clip
    torch.nn.utils.clip_grad_norm(net.parameters(), 1.0)

    optimizer.step()

    return outputs_grad, loss_grad


def cubic_train(model, criterion, optimizer, epoch):
    """
        Run one train epoch
    """
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()

    # switch to train mode
    model.train()

    end = time.time()
    for i, (input, target) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)
        target = target.cuda(async=True)

        # data for gradient computation in cubic
        input_var = torch.autograd.Variable(input)
        target_var = torch.autograd.Variable(target)
        if args.half:
            input_var = input_var.half()

        # # compute output
        # output = model(input_var)
        # loss = criterion(output, target_var)

        # data for Hessian-vector product computation in cubic
        inputs2, targets2 = next(iter(trainloader_cubic_hv))
        inputs_hv, targets_hv = torch.autograd.Variable(inputs2.cuda()), torch.autograd.Variable(targets2.cuda())
        output, loss = cubic_approx(model, epoch, optimizer, criterion, input_var, target_var, inputs_hv, targets_hv)

        # # compute gradient and do SGD step
        # optimizer.zero_grad()
        # loss.backward()
        # optimizer.step()

        output = output.float()
        loss = loss.float()
        # measure accuracy and record loss
        prec1 = accuracy(output.data, target)[0]
        losses.update(loss.data[0], input.size(0))
        top1.update(prec1[0], input.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            print(('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})'.format(
                      epoch, i, len(train_loader), batch_time=batch_time,
                      data_time=data_time, loss=losses, top1=top1)))


def train(train_loader, model, criterion, optimizer, epoch):
    """
        Run one train epoch
    """
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()

    # switch to train mode
    model.train()

    end = time.time()
    for i, (input, target) in enumerate(train_loader):

        # measure data loading time
        data_time.update(time.time() - end)

        target = target.cuda(async=True)
        input_var = torch.autograd.Variable(input).cuda()
        target_var = torch.autograd.Variable(target)
        if args.half:
            input_var = input_var.half()

        # compute output
        output = model(input_var)
        loss = criterion(output, target_var)

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        output = output.float()
        loss = loss.float()
        # measure accuracy and record loss
        prec1 = accuracy(output.data, target)[0]
        losses.update(loss.data[0], input.size(0))
        top1.update(prec1[0], input.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            print(('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})'.format(
                      epoch, i, len(train_loader), batch_time=batch_time,
                      data_time=data_time, loss=losses, top1=top1)))


def validate(val_loader, model, criterion):
    """
    Run evaluation
    """
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()

    # switch to evaluate mode
    model.eval()

    end = time.time()
    for i, (input, target) in enumerate(val_loader):
        target = target.cuda(async=True)
        input_var = torch.autograd.Variable(input, volatile=True).cuda()
        target_var = torch.autograd.Variable(target, volatile=True)

        if args.half:
            input_var = input_var.half()

        # compute output
        output = model(input_var)
        loss = criterion(output, target_var)

        output = output.float()
        loss = loss.float()

        # measure accuracy and record loss
        prec1 = accuracy(output.data, target)[0]
        losses.update(loss.data[0], input.size(0))
        top1.update(prec1[0], input.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            print(('Test: [{0}/{1}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})'.format(
                      i, len(val_loader), batch_time=batch_time, loss=losses,
                      top1=top1)))

    print((' * Prec@1 {top1.avg:.3f}'
          .format(top1=top1)))

    return top1.avg

def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    """
    Save the training model
    """
    torch.save(state, filename)

class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def adjust_learning_rate(optimizer, epoch):
    """Sets the learning rate to the initial LR decayed by 2 every 30 epochs"""
    lr = args.lr * (0.2 ** (epoch // 50))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr




def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


if __name__ == '__main__':
    main()
