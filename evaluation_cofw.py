import segmentation_models_pytorch as smp
import torch
from torch import nn
from torchvision import transforms as TF
import numpy as np
import cv2
from Dataset.utils import tensor2img
from PIL import Image
import os
from collections import OrderedDict
import tqdm
import imageio as iio

ENCODER = 'resnet18'
ENCODER_WEIGHTS = 'imagenet'
CLASSES = 1
ATTENTION = None
ACTIVATION = None
DEVICE = 'cude:0'
root = '/isilon/Datasets/FaceOcc/COFW_test/img'
root_mask = '/isilon/Datasets/FaceOcc/COFW_test/mask'
to_tensor = TF.ToTensor()
model = smp.Unet(encoder_name=ENCODER,
                 encoder_weights=ENCODER_WEIGHTS,
                 classes=CLASSES,
                 activation=ACTIVATION)

# model = nn.DataParallel(model.to(DEVICE), device_ids=[0, 1])
weights = torch.load('checkpoints/epoch_16_best.ckpt')
new_weights = OrderedDict()
for key in weights.keys():
    new_key = '.'.join(key.split('.')[1:])
    new_weights[new_key] = weights[key]

model.load_state_dict(new_weights)
model.to(DEVICE)
model.eval()
img_lst = os.listdir(root)


def load_data(name):
    img_pth = os.path.join(root, name)
    I = Image.open(img_pth)
    mask_name = name.split('.')[0] + '.png'
    mask_pth = os.path.join(root_mask, mask_name)
    mask = cv2.imread(mask_pth, 0)
    mask = mask // 255
    return to_tensor(I).unsqueeze(0), mask


def calc_iou(pred, labeled):
    labeled = labeled.squeeze()
    pred = pred.squeeze()
    inter = (pred * labeled).sum()
    # union = ((pred + labeled) > 0).sum()
    union = pred.sum() + labeled.sum() - inter
    iou = inter * 1.0 / union
    return iou


def calc_acc(pred, labeled):
    labeled = labeled.squeeze()
    pred = pred.squeeze()
    true = (pred == labeled).sum()
    acc = true / pred.flatten().shape[0]
    return acc


def calc_recall(pred, labeled):
    labeled = labeled.squeeze()
    pred = pred.squeeze()
    inter = (pred * labeled).sum()
    recall = inter / labeled.sum()
    return recall


def get_intermedia(data, pred_mask):
    data = torch.clone(data)
    data[:, 1:2, :, :] += pred_mask * 0.4
    data = torch.clamp(data, 0, 1)
    return tensor2img(data)


def get_refine(name):
    name = name.split('.')[0] + '.png'
    I = cv2.imread('iCloud/{}'.format(name), -1)
    I = cv2.resize(I, (256, 256))
    mask = I[..., 3] > 200
    mask = torch.from_numpy(mask.astype('float32')).unsqueeze(0).unsqueeze(0)
    mask = mask.cuda()
    return mask


def create_panel(img, pred_mask):
    mask = np.tile(pred_mask[..., None], (1, 1, 3)) * 255.0
    masked_img = img.copy()
    masked_img[:, :, 0] = np.clip(masked_img[:, :, 0] + mask[:, :, 0], 0, 255.0)
    out = np.hstack([img, masked_img, mask])
    return out


def run_cofw_test():
    total_iou = 0
    total_acc = 0
    total_recall = 0
    for name in tqdm.tqdm(img_lst):
        # print(name)

        data, gt_mask = load_data(name)
        data = data.to(DEVICE)
        with torch.no_grad():
            pred = model(data)

        pred_mask = (pred > 0).type(torch.int8)
        pred_mask = pred_mask.squeeze().cpu().numpy()

        # img = np.moveaxis(data.cpu().numpy()[0], (0, 1, 2), (2, 0, 1))
        # out = create_panel(img, pred_mask)
        # plt.figure()
        # plt.imshow(out)
        # plt.show()

        current_iou = calc_iou(pred_mask, gt_mask)
        current_acc = calc_acc(pred_mask, gt_mask)
        current_recall = calc_recall(pred_mask, gt_mask)
        total_iou += current_iou
        total_acc += current_acc
        total_recall += current_recall

    print('iou={}'.format(total_iou / len(img_lst)))
    print('acc={}'.format(total_acc / len(img_lst)))
    print('recall={}'.format(total_recall / len(img_lst)))

    # FPS evaluation
    dummy_input = torch.rand(1, 3, 256, 256).to(DEVICE)
    repetitions = 1000
    timings = np.zeros((repetitions, 1))
    starter = torch.cuda.Event(enable_timing=True)
    ender = torch.cuda.Event(enable_timing=True)

    # warm up
    with torch.no_grad():
        for _ in range(100):
            _ = model(dummy_input)

    torch.cuda.synchronize()

    # test
    with torch.no_grad():
        for rep in tqdm.tqdm(range(repetitions)):
            starter.record()
            _ = model(dummy_input)
            ender.record()
            torch.cuda.synchronize()
            curr_time = starter.elapsed_time(ender)
            timings[rep] = curr_time

    total_time = timings.sum() / 1000.  # millisecond to second
    fps = repetitions / (total_time)
    print('fps={}'.format(fps))


def predict_batch(frames):

    batch = torch.from_numpy(np.stack(frames, axis=0)).to(DEVICE)
    with torch.no_grad():
        pred = model(batch)

    pred_mask = (pred > 0).type(torch.int8)
    pred_mask = pred_mask.squeeze().cpu().numpy()
    return pred_mask


def run_on_videos(in_path, out_dir):

    os.makedirs(out_dir, exist_ok=True)

    for i, vid_name in enumerate(os.listdir(in_path)):
        print(f"starting {vid_name}")
        vout = iio.get_writer(
            os.path.join(out_dir, vid_name), fps=25, macro_block_size=1
        )
        vid_reader = iio.get_reader(os.path.join(in_path, vid_name))
        for j, img in enumerate(vid_reader):
            img = np.moveaxis(img/255, (0, 1, 2), (1, 2, 0))[None, ...]
            img = torch.from_numpy(img).to(DEVICE).float()
            with torch.no_grad():
                pred = model(img)

            pred_mask = (pred > 0).type(torch.int8)
            pred_mask = pred_mask.squeeze().cpu().numpy()
            img = np.moveaxis(img.cpu().numpy()[0], (0, 1, 2), (2, 0, 1))
            out = create_panel(img*255, pred_mask)
            vout.append_data(np.uint8(out))

        vout.close()


if __name__ == "__main__":
    # run_cofw_test()
    import time

    in_path = "/isilon/Datasets/shaked/occlusions_dataset/cropped_1.6_size_512"
    run_on_videos(in_path, "/isilon/Datasets/shaked/occlusions_dataset/faceocc/512")

# time.strftime("%Y%m%d_%H%M%S")
