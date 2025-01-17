import os
import ctypes
import platform
import scanpy as sc
import pandas as pd
from anndata import AnnData
from scipy.spatial.distance import cdist
import torch
import mkl
import numpy as np
from scipy.optimize import linear_sum_assignment
from sklearn import metrics
from torch.utils.data import DataLoader
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
mkl.get_max_threads()
C_DIP_FILE = None
def cluster_acc(y_true, y_pred):
    """
    Calculate clustering accuracy. Require scikit-learn installed

    # Arguments
        y: true labels, numpy.array with shape `(n_samples,)`
        y_pred: predicted labels, numpy.array with shape `(n_samples,)`

    # Return
        accuracy, in [0,1]
    """
    y_true = y_true.astype(np.int64)
    assert y_pred.size == y_true.size
    D = max(y_pred.max(), y_true.max()) + 1
    w = np.zeros((D, D), dtype=np.int64)
    for i in range(y_pred.size):
        w[y_pred[i], y_true[i]] += 1
    ind = linear_sum_assignment(w.max() - w)
    ind = np.array((ind[0], ind[1])).T
    return sum([w[i, j] for i, j in ind]) * 1.0 / y_pred.size


def Purity_score(y_true, y_pred):
    y_voted_labels = np.zeros(y_true.shape)
    labels = np.unique(y_true)
    ordered_labels = np.arange(labels.shape[0])
    for k in range(labels.shape[0]):
        y_true[y_true==labels[k]] = ordered_labels[k]
    labels = np.unique(y_true)
    bins = np.concatenate((labels, [np.max(labels)+1]), axis=0)

    for cluster in np.unique(y_pred):
        hist, _ = np.histogram(y_true[y_pred==cluster], bins=bins)
        winner = np.argmax(hist)
        y_voted_labels[y_pred==cluster] = winner

    purity = metrics.accuracy_score(y_true, y_voted_labels)
    return purity
def create_data_loader(datasets, batch_size, init=False, labels=None):
    if init:
        return DataLoader(datasets, batch_size=batch_size)
    if labels is not None:
        datasets.labels = torch.tensor(labels)
        datasets.need_target = True
        return DataLoader(datasets, batch_size=batch_size)
    else:
        datasets.need_target = False
        return DataLoader(datasets, batch_size=batch_size)



def detect_device():
    """Automatically detects if you have a cuda enabled GPU"""
    if torch.cuda.is_available():
        device = torch.device('cuda')
    else:
        device = torch.device('cpu')
    return device


def encode_batchwise(dataloader, model, device):
    """ Utility function for embedding the whole data set in a mini-batch fashion
    """
    embeddings = []
    for batch_idx, (xs, _) in enumerate(dataloader):
        for v in range(2):
            xs[v] = torch.squeeze(xs[v]).to(device)
        emb = model.encode(xs)
        embeddings.append(emb.detach().cpu())
    return torch.cat(embeddings, dim=0).numpy()


def int_to_one_hot(label_tensor, n_labels):
    onehot = torch.zeros([label_tensor.shape[0], n_labels], dtype=torch.float, device=label_tensor.device)
    onehot.scatter_(1, label_tensor.unsqueeze(1).long(), 1.0)
    return onehot


def squared_euclidean_distance(centers, embedded, weights=None):
    ta = centers.unsqueeze(0)
    tb = embedded.unsqueeze(1)
    squared_diffs = (ta - tb)
    if weights is not None:
        weights_unsqueezed = weights.unsqueeze(0).unsqueeze(1)
        squared_diffs = squared_diffs * weights_unsqueezed
    squared_diffs = squared_diffs.pow(2).sum(2)
    return squared_diffs


def get_nearest_points_to_optimal_centers(X, optimal_centers, embedded_data):
    centers_cpu = []
    best_center_points = np.argmin(cdist(optimal_centers, embedded_data), axis=1)
    for v in range(2):
        a = np.array(X[v][best_center_points, :])
        centers_cpu.append(a)
    embedded_centers_cpu = embedded_data[best_center_points, :]
    return centers_cpu, embedded_centers_cpu


def get_nearest_points(points_in_larger_cluster, center, size_smaller_cluster, max_cluster_size_diff_factor,
                        min_sample_size):

    distances = cdist(points_in_larger_cluster, [center])
    nearest_points = np.argsort(distances, axis=0)
    sample_size = size_smaller_cluster * max_cluster_size_diff_factor
    if size_smaller_cluster + sample_size < min_sample_size:
        sample_size = min(min_sample_size - size_smaller_cluster, len(points_in_larger_cluster))
    subset_all_points = points_in_larger_cluster[nearest_points[:sample_size, 0]]
    return subset_all_points


def judge_system():
    """
    Since results of ADClust slightly change under different operating environments,
    we take different activation functions for them.

    We used Ubuntu operating system with 16.04 version
    :return:
    """

    if platform.system() == "Windows":
        return False

    sys="linux"
    version="1.0"
    try:
        import lsb_release_ex as lsb
        info=lsb.get_lsb_information()
        sys=info['ID']
        version=info['RELEASE']
    except Exception as e:
        return False

    return (sys, version) == ('ubuntu','V10')


def get_center_labels(features, resolution=3.0):
    '''
    resolution: Value of the resolution parameter, use a value above
          (below) 1.0 if you want to obtain a larger (smaller) number
          of communities.
    '''

    print("\nInitializing cluster centroids using the louvain method.")

    adata0 = AnnData(features)
    sc.pp.neighbors(adata0, n_neighbors=15, use_rep="X")
    adata0 = sc.tl.louvain(adata0, resolution=resolution, random_state=0, copy=True)
    y_pred = adata0.obs['louvain']
    y_pred = np.asarray(y_pred, dtype=int)

    features = pd.DataFrame(adata0.X, index=np.arange(0, adata0.shape[0]))
    Group = pd.Series(y_pred, index=np.arange(0, adata0.shape[0]), name="Group")
    Mergefeature = pd.concat([features, Group], axis=1)

    init_centroid = np.asarray(Mergefeature.groupby("Group").mean())
    n_clusters = init_centroid.shape[0]

    #print("\n " + str(n_clusters) + " micro-clusters detected. \n")
    return init_centroid, y_pred



def dip_pval(data_dip, n_points):
    N, SIG, CV = dip_table_values()
    i1 = N.searchsorted(n_points, side='left')
    i0 = i1 - 1
    i0 = max(0, i0)
    i1 = min(N.shape[0] - 1, i1)
    # interpolate on sqrt(n)
    if i0 == i1 and i0 == N.shape[0] - 1:
        i0 = i1-1

    n0, n1 = N[[i0, i1]]
    fn = float(n_points - n0) / (n1 - n0)
    y0 = np.sqrt(n0) * CV[i0]
    y1 = np.sqrt(n1) * CV[i1]
    sD = np.sqrt(n_points) * data_dip
    pval = 1. - np.interp(sD, y0 + fn * (y1 - y0), SIG)
    return pval


def dip_table_values():
    N = np.array([4, 5, 6, 7, 8, 9, 10, 15, 20,
                  30, 50, 100, 200, 500, 1000, 2000, 5000, 10000,
                  20000, 40000, 72000])

    SIG = np.array([0., 0.01, 0.02, 0.05, 0.1, 0.2,
                    0.3, 0.4, 0.5, 0.6, 0.7, 0.8,
                    0.9, 0.95, 0.98, 0.99, 0.995, 0.998,
                    0.999, 0.9995, 0.9998, 0.9999, 0.99995, 0.99998,
                    0.99999, 1.])

    #  table of critical values from https://github.com/alimuldal/diptest
    # ,and https://github.com/tatome/dip_test
    # [len(N), len(SIG)] table of critical values
    CV = np.array([[0.125, 0.125, 0.125, 0.125, 0.125, 0.125, 0.125, 0.125, 0.125, 0.125, 0.132559548782689,
                    0.157497369040235, 0.187401878807559, 0.20726978858736, 0.223755804629222, 0.231796258864192,
                    0.237263743826779, 0.241992892688593, 0.244369839049632, 0.245966625504691, 0.247439597233262,
                    0.248230659656638, 0.248754269146416, 0.249302039974259, 0.249459652323225, 0.24974836247845],
                   [0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.108720593576329, 0.121563798026414, 0.134318918697053,
                    0.147298798976252, 0.161085025702604, 0.176811998476076, 0.186391796027944, 0.19361253363045,
                    0.196509139798845, 0.198159967287576, 0.199244279362433, 0.199617527406166, 0.199800941499028,
                    0.199917081834271, 0.199959029093075, 0.199978395376082, 0.199993151405815, 0.199995525025673,
                    0.199999835639211],
                   [0.0833333333333333, 0.0833333333333333, 0.0833333333333333, 0.0833333333333333,
                    0.0833333333333333, 0.0924514470941933, 0.103913431059949, 0.113885220640212, 0.123071187137781,
                    0.13186973390253, 0.140564796497941, 0.14941924112913, 0.159137064572627, 0.164769608513302,
                    0.179176547392782, 0.191862827995563, 0.202101971042968, 0.213015781111186, 0.219518627282415,
                    0.224339047394446, 0.229449332154241, 0.232714530449602, 0.236548128358969, 0.2390887911995,
                    0.240103566436295, 0.244672883617768],
                   [0.0714285714285714, 0.0714285714285714, 0.0714285714285714, 0.0725717816250742,
                    0.0817315478539489, 0.09405901819225269, 0.103244490800871, 0.110964599995697,
                    0.117807846504335, 0.124216086833531, 0.130409013968317, 0.136639642123068, 0.144240669035124,
                    0.159903395678336, 0.175196553271223, 0.184118659121501, 0.191014396174306, 0.198216795232182,
                    0.202341010748261, 0.205377566346832, 0.208306562526874, 0.209866047852379, 0.210967576933451,
                    0.212233348558702, 0.212661038312506, 0.21353618608817],
                   [0.0625, 0.0625, 0.06569119945032829, 0.07386511360717619, 0.0820045917762512,
                    0.0922700601131892, 0.09967371895993631, 0.105733531802737, 0.111035129847705,
                    0.115920055749988, 0.120561479262465, 0.125558759034845, 0.141841067033899, 0.153978303998561,
                    0.16597856724751, 0.172988528276759, 0.179010413496374, 0.186504388711178, 0.19448404115794,
                    0.200864297005026, 0.208849997050229, 0.212556040406219, 0.217149174137299, 0.221700076404503,
                    0.225000835357532, 0.233772919687683],
                   [0.0555555555555556, 0.0613018090298924, 0.0658615858179315, 0.0732651142535317,
                    0.0803941629593475, 0.0890432420913848, 0.0950811420297928, 0.09993808978110461,
                    0.104153560075868, 0.108007802361932, 0.112512617124951, 0.122915033480817, 0.136412639387084,
                    0.146603784954019, 0.157084065653166, 0.164164643657217, 0.172821674582338, 0.182555283567818,
                    0.188658833121906, 0.194089120768246, 0.19915700809389, 0.202881598436558, 0.205979795735129,
                    0.21054115498898, 0.21180033095039, 0.215379914317625],
                   [0.05, 0.0610132555623269, 0.0651627333214016, 0.0718321619656165, 0.077966212182459,
                    0.08528353598345639, 0.09032041737070989, 0.0943334983745117, 0.0977817630384725,
                    0.102180866696628, 0.109960948142951, 0.118844767211587, 0.130462149644819, 0.139611395137099,
                    0.150961728882481, 0.159684158858235, 0.16719524735674, 0.175419540856082, 0.180611195797351,
                    0.185286416050396, 0.191203083905044, 0.195805159339184, 0.20029398089673, 0.205651089646219,
                    0.209682048785853, 0.221530282182963],
                   [0.0341378172277919, 0.0546284208048975, 0.0572191260231815, 0.0610087367689692,
                    0.06426571373304441, 0.06922341079895911, 0.0745462114365167, 0.07920308789817621,
                    0.083621033469191, 0.08811984822029049, 0.093124666680253, 0.0996694393390689,
                    0.110087496900906, 0.118760769203664, 0.128890475210055, 0.13598356863636, 0.142452483681277,
                    0.150172816530742, 0.155456133696328, 0.160896499106958, 0.166979407946248, 0.17111793515551,
                    0.175900505704432, 0.181856676013166, 0.185743454151004, 0.192240563330562],
                   [0.033718563622065, 0.0474333740698401, 0.0490891387627092, 0.052719998201553,
                    0.0567795509056742, 0.0620134674468181, 0.06601638720690479, 0.06965060750664009,
                    0.07334377405927139, 0.07764606628802539, 0.0824558407118372, 0.08834462700173699,
                    0.09723460181229029, 0.105130218270636, 0.114309704281253, 0.120624043335821, 0.126552378036739,
                    0.13360135382395, 0.138569903791767, 0.14336916123968, 0.148940116394883, 0.152832538183622,
                    0.156010163618971, 0.161319225839345, 0.165568255916749, 0.175834459522789],
                   [0.0262674485075642, 0.0395871890405749, 0.0414574606741673, 0.0444462614069956,
                    0.0473998525042686, 0.0516677370374349, 0.0551037519001622, 0.058265005347493,
                    0.0614510857304343, 0.0649164408053978, 0.0689178762425442, 0.0739249074078291,
                    0.08147913793901269, 0.0881689143126666, 0.0960564383013644, 0.101478558893837,
                    0.10650487144103, 0.112724636524262, 0.117164140184417, 0.121425859908987, 0.126733051889401,
                    0.131198578897542, 0.133691739483444, 0.137831637950694, 0.141557509624351, 0.163833046059817],
                   [0.0218544781364545, 0.0314400501999916, 0.0329008160470834, 0.0353023819040016,
                    0.0377279973102482, 0.0410699984399582, 0.0437704598622665, 0.0462925642671299,
                    0.048851155289608, 0.0516145897865757, 0.0548121932066019, 0.0588230482851366,
                    0.06491363240467669, 0.0702737877191269, 0.07670958860791791, 0.0811998415355918,
                    0.0852854646662134, 0.09048478274902939, 0.0940930106666244, 0.0974904344916743,
                    0.102284204283997, 0.104680624334611, 0.107496694235039, 0.11140887547015, 0.113536607717411,
                    0.117886716865312],
                   [0.0164852597438403, 0.022831985803043, 0.0238917486442849, 0.0256559537977579,
                    0.0273987414570948, 0.0298109370830153, 0.0317771496530253, 0.0336073821590387,
                    0.0354621760592113, 0.0374805844550272, 0.0398046179116599, 0.0427283846799166,
                    0.047152783315718, 0.0511279442868827, 0.0558022052195208, 0.059024132304226,
                    0.0620425065165146, 0.06580160114660991, 0.0684479731118028, 0.0709169443994193,
                    0.0741183486081263, 0.0762579402903838, 0.0785735967934979, 0.08134583568891331,
                    0.0832963013755522, 0.09267804230967371],
                   [0.0111236388849688, 0.0165017735429825, 0.0172594157992489, 0.0185259426032926,
                    0.0197917612637521, 0.0215233745778454, 0.0229259769870428, 0.024243848341112,
                    0.025584358256487, 0.0270252129816288, 0.0286920262150517, 0.0308006766341406,
                    0.0339967814293504, 0.0368418413878307, 0.0402729850316397, 0.0426864799777448,
                    0.044958959158761, 0.0477643873749449, 0.0497198001867437, 0.0516114611801451,
                    0.0540543978864652, 0.0558704526182638, 0.0573877056330228, 0.0593365901653878,
                    0.0607646310473911, 0.0705309107882395],
                   [0.00755488597576196, 0.0106403461127515, 0.0111255573208294, 0.0119353655328931,
                    0.0127411306411808, 0.0138524542751814, 0.0147536004288476, 0.0155963185751048,
                    0.0164519238025286, 0.017383057902553, 0.0184503949887735, 0.0198162679782071,
                    0.0218781313182203, 0.0237294742633411, 0.025919578977657, 0.0274518022761997,
                    0.0288986369564301, 0.0306813505050163, 0.0320170996823189, 0.0332452747332959,
                    0.0348335698576168, 0.0359832389317461, 0.0369051995840645, 0.0387221159256424,
                    0.03993025905765, 0.0431448163617178],
                   [0.00541658127872122, 0.00760286745300187, 0.007949878346447991, 0.008521651834359399,
                    0.00909775605533253, 0.009889245210140779, 0.0105309297090482, 0.0111322726797384,
                    0.0117439009052552, 0.012405033293814, 0.0131684179320803, 0.0141377942603047,
                    0.0156148055023058, 0.0169343970067564, 0.018513067368104, 0.0196080260483234,
                    0.0206489568587364, 0.0219285176765082, 0.0228689168972669, 0.023738710122235,
                    0.0248334158891432, 0.0256126573433596, 0.0265491336936829, 0.027578430100536, 0.0284430733108,
                    0.0313640941982108],
                   [0.00390439997450557, 0.00541664181796583, 0.00566171386252323, 0.00607120971135229,
                    0.0064762535755248, 0.00703573098590029, 0.00749421254589299, 0.007920878896017331,
                    0.008355737247680061, 0.00882439333812351, 0.00936785820717061, 0.01005604603884,
                    0.0111019116837591, 0.0120380990328341, 0.0131721010552576, 0.0139655122281969,
                    0.0146889122204488, 0.0156076779647454, 0.0162685615996248, 0.0168874937789415,
                    0.0176505093388153, 0.0181944265400504, 0.0186226037818523, 0.0193001796565433,
                    0.0196241518040617, 0.0213081254074584],
                   [0.00245657785440433, 0.00344809282233326, 0.00360473943713036, 0.00386326548010849,
                    0.00412089506752692, 0.00447640050137479, 0.00476555693102276, 0.00503704029750072,
                    0.00531239247408213, 0.00560929919359959, 0.00595352728377949, 0.00639092280563517,
                    0.00705566126234625, 0.0076506368153935, 0.00836821687047215, 0.008863578928549141,
                    0.00934162787186159, 0.009932186363240289, 0.0103498795291629, 0.0107780907076862,
                    0.0113184316868283, 0.0117329446468571, 0.0119995948968375, 0.0124410052027886,
                    0.0129467396733128, 0.014396063834027],
                   [0.00174954269199566, 0.00244595133885302, 0.00255710802275612, 0.00273990955227265,
                    0.0029225480567908, 0.00317374638422465, 0.00338072258533527, 0.00357243876535982,
                    0.00376734715752209, 0.00397885007249132, 0.00422430013176233, 0.00453437508148542,
                    0.00500178808402368, 0.00542372242836395, 0.00592656681022859, 0.00628034732880374,
                    0.00661030641550873, 0.00702254699967648, 0.00731822628156458, 0.0076065423418208,
                    0.00795640367207482, 0.008227052458435399, 0.00852240989786251, 0.00892863905540303,
                    0.009138539330002131, 0.009522345795667729],
                   [0.00119458814106091, 0.00173435346896287, 0.00181194434584681, 0.00194259470485893,
                    0.00207173719623868, 0.00224993202086955, 0.00239520831473419, 0.00253036792824665,
                    0.00266863168718114, 0.0028181999035216, 0.00299137548142077, 0.00321024899920135,
                    0.00354362220314155, 0.00384330190244679, 0.00420258799378253, 0.00445774902155711,
                    0.00469461513212743, 0.00499416069129168, 0.00520917757743218, 0.00540396235924372,
                    0.00564540201704594, 0.00580460792299214, 0.00599774739593151, 0.00633099254378114,
                    0.00656987109386762, 0.00685829448046227],
                   [0.000852415648011777, 0.00122883479310665, 0.00128469304457018, 0.00137617650525553,
                    0.00146751502006323, 0.00159376453672466, 0.00169668445506151, 0.00179253418337906,
                    0.00189061261635977, 0.00199645471886179, 0.00211929748381704, 0.00227457698703581,
                    0.00250999080890397, 0.00272375073486223, 0.00298072958568387, 0.00315942194040388,
                    0.0033273652798148, 0.00353988965698579, 0.00369400045486625, 0.00383345715372182,
                    0.00400793469634696, 0.00414892737222885, 0.0042839159079761, 0.00441870104432879,
                    0.00450818604569179, 0.00513477467565583],
                   [0.000644400053256997, 0.000916872204484283, 0.000957932946765532, 0.00102641863872347,
                    0.00109495154218002, 0.00118904090369415, 0.00126575197699874, 0.00133750966361506,
                    0.00141049709228472, 0.00148936709298802, 0.00158027541945626, 0.00169651643860074,
                    0.00187306184725826, 0.00203178401610555, 0.00222356097506054, 0.00235782814777627,
                    0.00248343580127067, 0.00264210826339498, 0.0027524322157581, 0.0028608570740143,
                    0.00298695044508003, 0.00309340092038059, 0.00319932767198801, 0.00332688234611187,
                    0.00339316094477355, 0.00376331697005859]])
    return N, SIG, CV

def dip_test(X, is_data_sorted=False, debug=False):
    n_points = X.shape[0]
    data_dip = dip(X, just_dip=True, is_data_sorted=is_data_sorted, debug=debug)
    pval = dip_pval(data_dip, n_points)
    return data_dip, pval

def dip(X, just_dip=False, is_data_sorted=False, debug=False):
    assert X.ndim == 1, "Data must be 1-dimensional for the dip-test. Your shape:{0}".format(X.shape)

    N = len(X)
    if not is_data_sorted:
        X = np.sort(X)
    if N < 4 or X[0] == X[-1]:
        d = 0.0
        return d if just_dip else (d, None, None)

    # Prepare data to match C data types
    if C_DIP_FILE is None:
        load_c_dip_file()
    X = np.asarray(X, dtype=np.float64)
    X_c = X.ctypes.data_as(ctypes.POINTER(ctypes.c_double))
    N_c = np.array([N]).ctypes.data_as(ctypes.POINTER(ctypes.c_int))
    dip_value = np.zeros(1, dtype=np.float).ctypes.data_as(ctypes.POINTER(ctypes.c_double))
    low_high = np.zeros(4).ctypes.data_as(ctypes.POINTER(ctypes.c_int))
    modal_triangle = np.zeros(3).ctypes.data_as(ctypes.POINTER(ctypes.c_int))
    gcm = np.zeros(N).ctypes.data_as(ctypes.POINTER(ctypes.c_int))
    lcm = np.zeros(N).ctypes.data_as(ctypes.POINTER(ctypes.c_int))
    mn = np.zeros(N).ctypes.data_as(ctypes.POINTER(ctypes.c_int))
    mj = np.zeros(N).ctypes.data_as(ctypes.POINTER(ctypes.c_int))
    debug_c = np.array([1 if debug else 0]).ctypes.data_as(ctypes.POINTER(ctypes.c_int))

    # Execute C dip test
    _ = C_DIP_FILE.diptst(X_c, N_c, dip_value, low_high, modal_triangle, gcm, lcm, mn, mj, debug_c)
    dip_value = dip_value[0]
    if just_dip:
        return dip_value
    else:
        low_high = (low_high[0], low_high[1], low_high[2], low_high[3])
        modal_triangle = (modal_triangle[0], modal_triangle[1], modal_triangle[2])
        return dip_value, low_high, modal_triangle

def load_c_dip_file():
    global C_DIP_FILE
    files_path = os.path.dirname(__file__)
    print(files_path)
    if platform.system() == "Windows":
        dip_compiled = files_path + "/dip.dll"
    else:
        dip_compiled =  "/home/dayuhu/code/singlecell/多视图/scMPC/dip.so"

    print(dip_compiled)
    if os.path.isfile(dip_compiled):
        # load c file
        try:
            C_DIP_FILE = ctypes.CDLL(dip_compiled)
            C_DIP_FILE.diptst.restype = None
            C_DIP_FILE.diptst.argtypes = [ctypes.POINTER(ctypes.c_double),
                                          ctypes.POINTER(ctypes.c_int),
                                          ctypes.POINTER(ctypes.c_double),
                                          ctypes.POINTER(ctypes.c_int),
                                          ctypes.POINTER(ctypes.c_int),
                                          ctypes.POINTER(ctypes.c_int),
                                          ctypes.POINTER(ctypes.c_int),
                                          ctypes.POINTER(ctypes.c_int),
                                          ctypes.POINTER(ctypes.c_int),
                                          ctypes.POINTER(ctypes.c_int)]
        except Exception as e:
            print("[WARNING] Error while loading the C compiled dip file.")
            raise e
    else:
        raise Exception("C compiled dip file can not be found.\n"
                        "On Linux execute: gcc -fPIC -shared -o dip.so dip.c\n"
                        "Or Please ensure the dip.so was added in your LD_LIBRARY_PATH correctly by executing \n"
                        "(export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:./dip.so)   in the current directory of the scMPC folder. \n")

def merge_by_dip_value(X, embedded_data, cluster_labels_cpu, dip_argmax, n_clusters_current, centers_cpu, embedded_centers_cpu):

    # Get points in clusters
    points_in_center_1 = len(cluster_labels_cpu[cluster_labels_cpu == dip_argmax[0]])
    points_in_center_2 = len(cluster_labels_cpu[cluster_labels_cpu == dip_argmax[1]])

    # update labels
    for j, l in enumerate(cluster_labels_cpu):
        if l == dip_argmax[0] or l == dip_argmax[1]:
            cluster_labels_cpu[j] = n_clusters_current - 1
        elif l < dip_argmax[0] and l < dip_argmax[1]:
            cluster_labels_cpu[j] = l
        elif l > dip_argmax[0] and l > dip_argmax[1]:
            cluster_labels_cpu[j] = l - 2
        else:
            cluster_labels_cpu[j] = l - 1

    # Find new center position
    optimal_new_center = (embedded_centers_cpu[dip_argmax[0]] * points_in_center_1 +
                          embedded_centers_cpu[dip_argmax[1]] * points_in_center_2) / (
                                 points_in_center_1 + points_in_center_2)
    new_center_cpu, new_embedded_center_cpu = get_nearest_points_to_optimal_centers(X, [optimal_new_center],
                                                                                     embedded_data)
    # Remove the two old centers and add the new one
    centers_cpu_tmp = []
    for arr in centers_cpu:
        tmp_arr = np.delete(arr, dip_argmax, axis=0)
        centers_cpu_tmp.append(tmp_arr)

    centers_cpu = []
    for w in range(2):
        b = np.append(centers_cpu_tmp[w], new_center_cpu[w], axis=0)
        centers_cpu.append(b)
    embedded_centers_cpu_tmp = np.delete(embedded_centers_cpu, dip_argmax, axis=0)
    embedded_centers_cpu = np.append(embedded_centers_cpu_tmp, new_embedded_center_cpu, axis=0)



    # Update dip values
    dip_matrix_cpu = get_dip_matrix(embedded_data, embedded_centers_cpu, cluster_labels_cpu, n_clusters_current)
    return cluster_labels_cpu, centers_cpu, embedded_centers_cpu, dip_matrix_cpu

def get_dip_matrix(data, dip_centers, dip_labels, n_clusters, max_cluster_size_diff_factor=3, min_sample_size=100):
    dip_matrix = np.zeros((n_clusters, n_clusters))

    # Loop over all combinations of centers
    for i in range(0, n_clusters - 1):
        for j in range(i + 1, n_clusters):
            center_diff = dip_centers[i] - dip_centers[j]
            points_in_i = data[dip_labels == i]
            points_in_j = data[dip_labels == j]
            points_in_i_or_j = np.append(points_in_i, points_in_j, axis=0)
            proj_points = np.dot(points_in_i_or_j, center_diff)
            _, dip_p_value = dip_test(proj_points)

            # Check if clusters sizes differ heavily
            if points_in_i.shape[0] > points_in_j.shape[0] * max_cluster_size_diff_factor or \
                    points_in_j.shape[0] > points_in_i.shape[0] * max_cluster_size_diff_factor:
                if points_in_i.shape[0] > points_in_j.shape[0] * max_cluster_size_diff_factor:
                    points_in_i = get_nearest_points(points_in_i, dip_centers[j], points_in_j.shape[0],
                                                      max_cluster_size_diff_factor, min_sample_size)
                elif points_in_j.shape[0] > points_in_i.shape[0] * max_cluster_size_diff_factor:
                    points_in_j = get_nearest_points(points_in_j, dip_centers[i], points_in_i.shape[0],
                                                      max_cluster_size_diff_factor, min_sample_size)
                points_in_i_or_j = np.append(points_in_i, points_in_j, axis=0)
                proj_points = np.dot(points_in_i_or_j, center_diff)
                _, dip_p_value_2 = dip_test(proj_points)
                dip_p_value = min(dip_p_value, dip_p_value_2)

            # Add pval to dip matrix
            dip_matrix[i][j] = dip_p_value
            dip_matrix[j][i] = dip_p_value

    return dip_matrix
