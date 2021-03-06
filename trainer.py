from __future__ import print_function
from six.moves import range
from PIL import Image

import torch.backends.cudnn as cudnn
import torch
import torch.nn as nn
import torch.optim as optim
import os
import time
import numpy as np
import torchfile

from utils import mkdir_p, weights_init, save_img_results, save_model
from utils import KL_loss, JSD_loss, compute_discriminator_loss, compute_generator_loss

from tensorboard import summary
from tensorboard import FileWriter

class GANTrainer(object):
    def __init__(self, output_dir,
                 max_epoch,
                 snapshot_interval,
                 gpu_id, batch_size,
                 train_flag, net_g, 
                 net_d, cuda, stage1_g,
                 z_dim, generator_lr,
                 discriminator_lr, lr_decay_epoch,
                 coef_kl, regularizer
                 ):
        if train_flag:
            self.model_dir = os.path.join(output_dir, 'Model')
            self.image_dir = os.path.join(output_dir, 'Image')
            self.log_dir = os.path.join(output_dir, 'Log')
            mkdir_p(self.model_dir)
            mkdir_p(self.image_dir)
            mkdir_p(self.log_dir)
            self.summary_writer = FileWriter(self.log_dir)

        self.max_epoch = max_epoch
        self.snapshot_interval = snapshot_interval
        self.net_g = net_g
        self.net_d = net_d
        self.cuda = cuda
        self.stage1_g = stage1_g
        self.nz = z_dim
        self.generator_lr = generator_lr
        self.discriminator_lr = discriminator_lr
        self.lr_decay_step = lr_decay_epoch
        self.coef_kl = coef_kl
        self.regularizer = regularizer
        
        s_gpus = gpu_id.split(',')
        self.gpus = [int(ix) for ix in s_gpus]
        self.num_gpus = len(self.gpus)
        self.batch_size = batch_size * self.num_gpus
        torch.cuda.set_device(self.gpus[0])
        cudnn.benchmark = True

    # ############# For training stageI GAN #############
    def load_network_stageI(self, text_dim, gf_dim, condition_dim, z_dim, df_dim):
        from model import STAGE1_G, STAGE1_D
        netG = STAGE1_G(text_dim, gf_dim, condition_dim, z_dim, self.cuda)
        netG.apply(weights_init)
        print(netG)
        netD = STAGE1_D(df_dim, condition_dim)
        netD.apply(weights_init)
        print(netD)

        if self.net_g != '':
            state_dict = torch.load(self.net_g)
            #torch.load(self.net_g, map_location=lambda storage, loc: storage)
            netG.load_state_dict(state_dict)
            print('Load from: ', self.net_g)
        if self.net_d != '':
            state_dict = torch.load(self.net_d, map_location=lambda storage, loc: storage)
            netD.load_state_dict(state_dict)
            print('Load from: ', self.net_d)
        if self.cuda:
            netG.cuda()
            netD.cuda()
        return netG, netD

    # ############# For training stageII GAN  #############
    def load_network_stageII(self, text_dim, gf_dim, condition_dim, z_dim, df_dim, res_num):
        from model import STAGE1_G, STAGE2_G, STAGE2_D

        Stage1_G = STAGE1_G(text_dim, gf_dim, condition_dim, z_dim, self.cuda)
        netG = STAGE2_G(Stage1_G, text_dim, gf_dim, condition_dim, z_dim, res_num, self.cuda)
        netG.apply(weights_init)
        print(netG)
        if self.net_g != '':
            state_dict = torch.load(self.net_g)
            #torch.loadself.net_g, map_location=lambda storage, loc: storage)
            netG.load_state_dict(state_dict)
            print('Load from: ', self.net_g)
        elif self.stage1_g != '':
            state_dict = torch.load(self.stage1_g, map_location=lambda storage, loc: storage)
            netG.STAGE1_G.load_state_dict(state_dict)
            print('Load from: ', self.stage1_g)
        else:
            print("Please give the Stage1_G path")
            return

        netD = STAGE2_D(df_dim, condition_dim)
        netD.apply(weights_init)
        if self.net_d != '':
            state_dict = torch.load(self.net_d, map_location=lambda storage, loc: storage)
            netD.load_state_dict(state_dict)
            print('Load from: ', self.net_d)
        print(netD)

        if self.cuda:
            netG.cuda()
            netD.cuda()
        return netG, netD

    def train(self, data_loader, stage, text_dim, gf_dim, condition_dim, z_dim, df_dim, res_num):
        if stage == 1:
            netG, netD = self.load_network_stageI(text_dim, gf_dim, condition_dim, z_dim, df_dim)
        else:
            netG, netD = self.load_network_stageII(text_dim, gf_dim, condition_dim, z_dim, df_dim, res_num)

        batch_size = self.batch_size
        noise = torch.FloatTensor(batch_size, self.nz)
        fixed_noise = torch.FloatTensor(batch_size, self.nz).normal_(0, 1)
        real_labels = torch.ones(batch_size)
        fake_labels = torch.zeros(batch_size)
        
        if self.cuda:
            noise, fixed_noise = noise.cuda(), fixed_noise.cuda()
            real_labels, fake_labels = real_labels.cuda(), fake_labels.cuda()

        optimizerD = optim.Adam(netD.parameters(),
                       lr=self.discriminator_lr, betas=(0.5, 0.999))
        netG_para = []
        for p in netG.parameters():
            if p.requires_grad:
                netG_para.append(p)
        optimizerG = optim.Adam(netG_para,
                                self.generator_lr,
                                betas=(0.5, 0.999))
        count = 0
        for epoch in range(self.max_epoch):
            start_t = time.time()
            if epoch % self.lr_decay_step == 0 and epoch > 0:
                self.generator_lr *= 0.5
                for param_group in optimizerG.param_groups:
                    param_group['lr'] = self.generator_lr
                self.discriminator_lr *= 0.5
                for param_group in optimizerD.param_groups:
                    param_group['lr'] = self.discriminator_lr

            for i, data in enumerate(data_loader, 0):
                ######################################################
                # (1) Prepare training data
                ######################################################
                real_imgs, txt_embedding = data
                if self.cuda:
                    real_imgs = real_imgs.cuda()
                    txt_embedding = txt_embedding.cuda()

                #######################################################
                # (2) Generate fake images
                ######################################################
                noise.data.normal_(0, 1)
                inputs = (txt_embedding, noise)
                _, fake_imgs, mu, logvar = \
                    nn.parallel.data_parallel(netG, inputs, self.gpus)

                ############################
                # (3) Update D network
                ###########################
                netD.zero_grad()
                errD, errD_real, errD_wrong, errD_fake = \
                    compute_discriminator_loss(netD, real_imgs, fake_imgs,
                                               real_labels, fake_labels,
                                               mu, self.gpus)
                errD.backward()
                optimizerD.step()
                ############################
                # (2) Update G network
                ###########################
                netG.zero_grad()
                errG = compute_generator_loss(netD, fake_imgs,
                                              real_labels, mu, self.gpus)
                
                if self.regularizer == 'KL':
                    regularizer_loss = KL_loss(mu, logvar)
                else:
                    regularizer_loss = JSD_loss(mu, logvar)

                errG_total = errG + regularizer_loss * self.coef_kl
                errG_total.backward()
                optimizerG.step()

                count = count + 1
                if i % 100 == 0:
                    summary_D = summary.scalar('D_loss', errD.item())
                    summary_D_r = summary.scalar('D_loss_real', errD_real)
                    summary_D_w = summary.scalar('D_loss_wrong', errD_wrong)
                    summary_D_f = summary.scalar('D_loss_fake', errD_fake)
                    summary_G = summary.scalar('G_loss', errG.item())
                    summary_KL = summary.scalar('Regularizer_loss', regularizer_loss.item())

                    self.summary_writer.add_summary(summary_D, count)
                    self.summary_writer.add_summary(summary_D_r, count)
                    self.summary_writer.add_summary(summary_D_w, count)
                    self.summary_writer.add_summary(summary_D_f, count)
                    self.summary_writer.add_summary(summary_G, count)
                    self.summary_writer.add_summary(summary_KL, count)

                    # save the image result for each epoch
                    inputs = (txt_embedding, fixed_noise)
                    lr_fake, fake, _, _ = \
                        nn.parallel.data_parallel(netG, inputs, self.gpus)
                    save_img_results(real_imgs.cpu(), fake, epoch, self.image_dir)
                    if lr_fake is not None:
                        save_img_results(None, lr_fake, epoch, self.image_dir)
            
            end_t = time.time()
            print('''[%d/%d][%d/%d] Loss_D: %.4f Loss_G: %.4f Loss_KL: %.4f
                     Loss_real: %.4f Loss_wrong:%.4f Loss_fake %.4f
                     Total Time: %.2fsec
                  '''
                  % (epoch, self.max_epoch, i, len(data_loader),
                     errD.item(), errG.item(), regularizer_loss.item(),
                     errD_real, errD_wrong, errD_fake, (end_t - start_t)))
            if epoch % self.snapshot_interval == 0:
                save_model(netG, netD, epoch, self.model_dir)
        #
        save_model(netG, netD, self.max_epoch, self.model_dir)
        #
        self.summary_writer.close()

    def sample(self, datapath, stage=1):
        if stage == 1:
            netG, _ = self.load_network_stageI()
        else:
            netG, _ = self.load_network_stageII()
        netG.eval()

        # Load text embeddings generated from the encoder
        t_file = torchfile.load(datapath)
        captions_list = t_file.raw_txt
        embeddings = np.concatenate(t_file.fea_txt, axis=0)
        num_embeddings = len(captions_list)
        print('Successfully load sentences from: ', datapath)
        print('Total number of sentences:', num_embeddings)
        print('num_embeddings:', num_embeddings, embeddings.shape)
        # path to save generated samples
        save_dir = self.net_g[:self.net_g.find('.pth')]
        mkdir_p(save_dir)

        batch_size = np.minimum(num_embeddings, self.batch_size)
        noise = torch.FloatTensor(batch_size, self.nz)
        if self.cuda:
            noise = noise.cuda()
        count = 0
        while count < num_embeddings:
            if count > 3000:
                break
            iend = count + batch_size
            if iend > num_embeddings:
                iend = num_embeddings
                count = num_embeddings - batch_size
            embeddings_batch = embeddings[count:iend]
            # captions_batch = captions_list[count:iend]
            txt_embedding = torch.FloatTensor(embeddings_batch)
            if self.cuda:
                txt_embedding = txt_embedding.cuda()

            #######################################################
            # (2) Generate fake images
            ######################################################
            noise.data.normal_(0, 1)
            inputs = (txt_embedding, noise)
            _, fake_imgs, mu, logvar = \
                nn.parallel.data_parallel(netG, inputs, self.gpus)
            for i in range(batch_size):
                save_name = '%s/%d.png' % (save_dir, count + i)
                im = fake_imgs[i].data.cpu().numpy()
                im = (im + 1.0) * 127.5
                im = im.astype(np.uint8)
                # print('im', im.shape)
                im = np.transpose(im, (1, 2, 0))
                # print('im', im.shape)
                im = Image.fromarray(im)
                im.save(save_name)
            count += batch_size

    def birds_eval(self, data_loader, stage=2):
        if stage == 1:
            netG, netD = self.load_network_stageI()
        else:
            netG, netD = self.load_network_stageII()
        netG.eval()
        
        nz = self.z_dim
        batch_size = self.batch_size
        noise = Variable(torch.FloatTensor(batch_size, nz))
        fixed_noise = Variable(torch.FloatTensor(batch_size, nz).normal_(0, 1), volatile=True)
        real_labels = Variable(torch.FloatTensor(batch_size).fill_(1))
        fake_labels = Variable(torch.FloatTensor(batch_size).fill_(0))
        if self.cuda:
            noise, fixed_noise = noise.cuda(), fixed_noise.cuda()
            real_labels, fake_labels = real_labels.cuda(), fake_labels.cuda()

        # path to save generated samples
        save_dir = self.netG[:self.netG.find('.pth')]
        print("Save directory", save_dir)
        mkdir_p(save_dir)
        
        count = 0
        for i, data in enumerate(data_loader, 0):
            ######################################################
            # (1) Prepare training data
            ######################################################
            real_img_cpu, txt_embedding = data
            real_imgs = Variable(real_img_cpu)
            txt_embedding = Variable(txt_embedding)
            if self.cuda:
                real_imgs = real_imgs.cuda()
                txt_embedding = txt_embedding.cuda()
            print("Batch Running:", i)
            #######################################################
            # (2) Generate fake images
            ######################################################
            noise.data.normal_(0, 1)
            inputs = (txt_embedding, noise)
            _, fake_imgs, mu, logvar = \
                nn.parallel.data_parallel(netG, inputs, self.gpus)
            for i in range(batch_size):
                save_name = '%s/%d.png' % (save_dir, count + i)
                im = fake_imgs[i].data.cpu().numpy()
                im = (im + 1.0) * 127.5
                im = im.astype(np.uint8)
                # print('im', im.shape)
                im = np.transpose(im, (1, 2, 0))
                # print('im', im.shape)
                im = Image.fromarray(im)
                im.save(save_name)
            count += batch_size   